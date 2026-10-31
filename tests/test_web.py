from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents import session
from agents.web import WebApp, WebError, WebServer, history_entries


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session_patch = patch.object(session, "SESSION_DIR", Path(self.temp.name))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.app = WebApp(options={"api_key": "fake-secret", "api_base": "http://example.test/v1", "api_protocol": "openai",
            "model": "test-model", "custom_system_prompt": "test", "enable_mcp": False,
            "enable_memory_prefetch": False, "enable_online_evolution": False})
        self.addCleanup(self.app.close)

    def dispatch(self, method, path, body=None):
        return self.app.call(self.app.dispatch(method,path,body))

    def new_view(self):
        state = self.dispatch("POST", "/api/sessions", {})
        return self.app.conversations[state["id"]]

    def wait_state(self, view, predicate):
        deadline = time.monotonic() + 10
        with view.condition:
            while not predicate(view.snapshot):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail("Timed out waiting for web state")
                view.condition.wait(remaining)
            return view.snapshot

    def test_streamed_turn_reuses_runtime_authorization_and_can_resume(self):
        view = self.new_view()
        async def install():
            view.ensure_agent()
            async def fake_turn(message):
                view.agent._openai_messages.append({"role":"user", "content":message})
                view.agent._emit_text("正在检查\n")
                result = await view.agent._run_checked_tool("write_file", {"file_path":"example.txt", "content":"text"})
                view.agent._emit_text(result)
                view.agent._openai_messages.append({"role":"assistant", "content":result})
            view.agent._chat_turn = fake_turn
            view.agent._execute_tool_call = AsyncMock(return_value="File written successfully")
        self.app.call(install())
        self.dispatch("POST", f"/api/sessions/{view.id}/chat", {"message":"检查权限"})
        state = self.wait_state(view, lambda state:bool(state["requests"]))
        view.agent._execute_tool_call.assert_not_awaited()
        req = state["requests"][0]
        self.assertEqual(req["toolId"],state["entries"][-1]["id"])
        self.dispatch("POST",f"/api/sessions/{view.id}/confirm",{"id":req["id"],"allow":True})
        state = self.wait_state(view, lambda state:not state["busy"])
        view.agent._execute_tool_call.assert_awaited_once()
        self.assertEqual(state["files"],["example.txt"])
        self.assertIn("正在检查",state["entries"][1]["text"])
        stored = session.load_session(view.id)
        self.assertEqual(stored["webEntries"],state["entries"])
        self.assertEqual(history_entries(stored),state["entries"])

    def test_denial_never_executes_tool(self):
        view = self.new_view()
        async def execute():
            view.ensure_agent()
            view.agent._execute_tool_call = AsyncMock(return_value="not expected")
            return await view.agent._run_checked_tool("run_shell", {"command":"echo not-executed"})
        future = asyncio.run_coroutine_threadsafe(execute(),self.app.loop)
        state = self.wait_state(view, lambda state:bool(state["requests"]))
        self.dispatch("POST", f"/api/sessions/{view.id}/confirm",{"id":state["requests"][0]["id"],"allow":False})
        self.assertIn("denied",future.result(3))
        view.agent._execute_tool_call.assert_not_awaited()

    def test_stop_cancels_pending_authorization(self):
        view = self.new_view()
        async def install():
            view.ensure_agent()
            async def fake_turn(message):
                await view.agent._run_checked_tool("run_shell", {"command":"echo test"})
            view.agent._chat_turn = fake_turn
            view.agent._execute_tool_call = AsyncMock()
        self.app.call(install())
        self.dispatch("POST", f"/api/sessions/{view.id}/chat",{"message":"test"})
        self.wait_state(view,lambda state:bool(state["requests"]))
        self.dispatch("POST",f"/api/sessions/{view.id}/stop",{})
        state = self.wait_state(view,lambda state:not state["busy"])
        self.assertFalse(state["requests"])
        self.assertEqual(state["entries"][-1]["status"],"cancelled")
        view.agent._execute_tool_call.assert_not_awaited()

    def test_only_one_project_turn_runs_at_once(self):
        first,second = self.new_view(),self.new_view()
        async def install():
            first.ensure_agent()
            first.agent._chat_turn = lambda message:asyncio.sleep(30)
        self.app.call(install())
        self.dispatch("POST",f"/api/sessions/{first.id}/chat",{"message":"test"})
        with self.assertRaisesRegex(WebError,"已有任务"):
            self.dispatch("POST",f"/api/sessions/{second.id}/chat",{"message":"test"})
        with self.assertRaisesRegex(WebError,"等待当前任务"):
            self.dispatch("POST",f"/api/sessions/{first.id}/mode",{"mode":"acceptEdits"})
        self.dispatch("POST",f"/api/sessions/{first.id}/stop",{})

    def test_modes_keep_plan_prompt_and_reject_bypass(self):
        view = self.new_view()
        async def install():
            view.ensure_agent()
        self.app.call(install())
        for mode in ("plan","acceptEdits","default"):
            state = self.dispatch("POST",f"/api/sessions/{view.id}/mode",{"mode":mode})
            self.assertEqual(state["mode"],mode)
            self.assertEqual(bool(view.agent._plan_file_path),mode == "plan")
        with self.assertRaises(WebError):
            self.dispatch("POST",f"/api/sessions/{view.id}/mode",{"mode":"bypassPermissions"})

    def test_no_key_still_allows_frontend_but_cannot_start_chat(self):
        self.app.options["api_key"] = None
        view = self.new_view()
        with self.assertRaisesRegex(WebError,"尚未配置"):
            self.dispatch("POST",f"/api/sessions/{view.id}/chat",{"message":"test"})
        self.assertFalse(view.busy)
        self.assertFalse(view.entries)
        self.assertIsNone(view.agent)

    def test_cross_project_sessions_cannot_be_read(self):
        session.save_session("other",{"metadata":{"id":"other","cwd":"/other-project"}})
        with self.assertRaises(WebError) as caught:
            self.dispatch("GET","/api/sessions/other")
        self.assertEqual(caught.exception.status,404)
        self.assertEqual(self.dispatch("GET","/api/sessions"),[])

    def test_history_hides_system_and_runtime_injections_and_keeps_tool_result(self):
        data = {"openaiMessages":[{"role":"system","content":"secret prompt"},{"role":"user","content":"task\n<retrieved_skills>hidden</retrieved_skills>"},
            {"role":"assistant","content":None,"tool_calls":[{"id":"call1","function":{"name":"read_file","arguments":"{\"file_path\":\"x\"}"}}]},
            {"role":"tool","tool_call_id":"call1","content":"the result"},{"role":"assistant","content":"done"}]}
        entries = history_entries(data)
        self.assertEqual(entries[0]["text"],"task")
        self.assertEqual(entries[1]["result"],"the result")
        self.assertEqual(entries[-1]["text"],"done")
        self.assertEqual(len(entries),3)

    def test_legacy_mixed_thinking_uses_saved_answer_without_text_guessing(self):
        data = {"webEntries":[{"kind":"message","id":"old","role":"assistant","text":"\n  [thinking] opaque analysis你好！"}],
                "anthropicMessages":[{"role":"user","content":"你好"},{"role":"assistant","content":[{"type":"text","text":"你好！"}]}]}
        entries = history_entries(data)
        self.assertEqual(entries[-1]["text"],"你好！")
        self.assertNotIn("opaque analysis",json.dumps(entries))

    def test_real_sdk_streams_keep_reasoning_out_of_answers_and_learning_buffers(self):
        import anthropic
        import httpx
        import openai
        for protocol in ("anthropic","openai"):
            with self.subTest(protocol=protocol):
                self.app.options["api_protocol"] = protocol
                view = self.new_view()
                def handler(request):
                    if protocol == "anthropic":
                        events = [
                            {"type":"message_start","message":{"id":"msg","type":"message","role":"assistant","model":"test-model","content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":1,"output_tokens":0}}},
                            {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}},
                            {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"整理问题"}},
                            {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"signature"}},
                            {"type":"content_block_stop","index":0},
                            {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}},
                            {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"你好！"}},
                            {"type":"content_block_stop","index":1},
                            {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":2}},
                            {"type":"message_stop"}]
                        body = ''.join('event: '+e["type"]+'\ndata: '+json.dumps(e)+'\n\n' for e in events)
                    else:
                        chunks = [
                            {"id":"msg","object":"chat.completion.chunk","created":0,"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"整理问题"},"finish_reason":None}]},
                            {"id":"msg","object":"chat.completion.chunk","created":0,"model":"test-model","choices":[{"index":0,"delta":{"content":"你好！"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}]
                        body = ''.join('data: '+json.dumps(e)+'\n\n' for e in chunks) + 'data: [DONE]\n\n'
                    return httpx.Response(200,headers={"content-type":"text/event-stream"},content=body)
                async def install():
                    view.ensure_agent()
                    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                    if protocol == "anthropic":
                        await view.agent._anthropic_client.close()
                        view.agent._anthropic_client = anthropic.AsyncAnthropic(api_key="fake",http_client=client)
                    else:
                        await view.agent._openai_client.close()
                        view.agent._openai_client = openai.AsyncOpenAI(api_key="fake",http_client=client)
                self.app.call(install())
                self.dispatch("POST",f"/api/sessions/{view.id}/chat",{"message":"你好"})
                state = self.wait_state(view,lambda state:not state["busy"])
                self.assertIsNone(state["error"])
                reasoning = [e for e in state["entries"] if e["kind"] == "thinking"]
                answers = [e["text"] for e in state["entries"] if e.get("role") == "assistant"]
                self.assertEqual(reasoning[0]["text"],"整理问题")
                self.assertEqual(reasoning[0]["status"],"done")
                self.assertEqual(''.join(answers).strip(),"你好！")
                self.assertNotIn("整理问题",str(view.agent._skill_eval_dialog))
                saved = session.load_session(view.id)
                self.assertNotIn("整理问题",json.dumps(saved.get("anthropicMessages") or saved.get("openaiMessages")))
                self.assertEqual(history_entries(saved),state["entries"])

    def test_plan_review_has_explicit_choices(self):
        view = self.new_view()
        future = asyncio.run_coroutine_threadsafe(view.approve_plan("the plan"),self.app.loop)
        req = self.wait_state(view,lambda s:bool(s["requests"]))["requests"][0]
        with self.assertRaises(WebError):
            self.dispatch("POST",f"/api/sessions/{view.id}/confirm",{"id":req["id"],"choice":"execute"})
        self.dispatch("POST",f"/api/sessions/{view.id}/confirm",{"id":req["id"],"choice":"keep-planning","feedback":"revise"})
        self.assertEqual(future.result(3),{"choice":"keep-planning","feedback":"revise"})

    def test_error_redacts_api_key(self):
        self.assertNotIn("fake-secret",self.app.safe_error(ValueError("bad key fake-secret")))

    def test_evaluation_cannot_write_in_plan_mode(self):
        view = self.new_view()
        async def execute():
            view.ensure_agent()
            view.agent.permission_mode = "plan"
            query = Mock()
            view.agent._build_side_query = query
            with self.assertRaisesRegex(WebError,"不允许写入"):
                await view.run_command("/skill-eval")
            query.assert_not_called()
            self.assertFalse(view.requests)
        self.app.call(execute())

    def test_candidate_publication_preserves_permission_gate(self):
        view = self.new_view()
        proposal = {"action":"add","skill":"example","write_input":{"name":"example","instructions":"body"}}
        async def execute():
            view.ensure_agent()
            view.agent.authorize_operation = AsyncMock(return_value=False)
            with patch("agents.skill_candidates.read_candidate", return_value=proposal), patch("agents.skill_candidates.apply_candidate") as apply:
                await view.run_command("/skill-apply " + "a"*24 + " --manual-review")
                apply.assert_not_called()
                view.agent.authorize_operation.assert_awaited_once_with("skill_create",proposal["write_input"])
        self.app.call(execute())

    def test_stream_retry_replaces_partial_web_answer_and_saved_learning_text(self):
        view = self.new_view()

        class Stream:
            def __init__(self, fail):
                self.fail = fail

            def __aiter__(self):
                return self.events()

            async def events(self):
                from types import SimpleNamespace as NS
                yield NS(usage=None, choices=[NS(delta=NS(content="FAILED_PARTIAL" if self.fail else "SUCCESS",
                                                         tool_calls=None), finish_reason=None if self.fail else "stop")])
                if self.fail:
                    raise RuntimeError("ECONNRESET")

            async def close(self):
                pass

        async def retry_once(request):
            try:
                return await request()
            except RuntimeError:
                return await request()

        async def run():
            view.ensure_agent()
            view.entries.append({"kind": "message", "id": "user", "role": "user", "text": "task"})
            view.agent._augment_user_message_with_skill_context = lambda text: (text, None)
            with patch.object(view.agent._openai_client.chat.completions, "create",
                              AsyncMock(side_effect=[Stream(True), Stream(False)])), \
                    patch("agents.agent._with_retry", retry_once):
                await view.agent.chat("task")

        self.app.call(run())
        stored = session.load_session(view.id)
        self.assertNotIn("FAILED_PARTIAL", json.dumps(stored))
        self.assertIn("SUCCESS", json.dumps(view.entries))
        self.assertEqual(stored["runtimeState"]["pendingSkillExtractionWindow"]["latest_assistant"], "SUCCESS")

    def test_restored_web_tools_show_uncertainty_or_durable_completion(self):
        saved = {"webEntries": [
            {"id": "ui-a", "callId": "a", "kind": "tool", "name": "write_file", "status": "running", "result": ""},
            {"id": "ui-b", "callId": "b", "kind": "tool", "name": "write_file", "status": "running", "result": ""},
        ], "runtimeState": {"toolCheckpoints": {
            "a": {"status": "running"}, "b": {"status": "completed", "result": "Successfully wrote"}}}}
        entries = history_entries(saved)
        self.assertEqual(entries[0]["status"], "error")
        self.assertIn("结果未知", entries[0]["result"])
        self.assertEqual(entries[1]["status"], "done")
        self.assertEqual(entries[1]["result"], "Successfully wrote")
        self.assertEqual(saved["webEntries"][0]["status"], "running")

    def start_server(self):
        server = WebServer(("127.0.0.1",0),self.app)
        worker = threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_http_assets_csrf_cookie_origin_and_host_guards(self):
        server = self.start_server()
        client = http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
        self.addCleanup(client.close)
        client.request("GET","/")
        response = client.getresponse()
        cookie = response.getheader("Set-Cookie").split(";")[0]
        self.assertIn(b'mawoagent',response.read())
        self.assertIn("HttpOnly",response.getheader("Set-Cookie"))
        self.assertIn("frame-ancestors 'none'",response.getheader("Content-Security-Policy"))
        client.request("GET","/api/config")
        response = client.getresponse()
        self.assertEqual(response.status,403)
        response.read()
        client.request("GET","/api/config",headers={"Cookie":cookie})
        response = client.getresponse()
        config = json.loads(response.read())
        self.assertNotIn("fake-secret",json.dumps(config))
        for extra in ({},{"Origin":"http://evil.example"},{"Host":"evil.example"},{"Sec-Fetch-Site":"cross-site"}):
            headers = {"Cookie":cookie,"Content-Type":"application/json"}
            if extra:
                headers["X-Mawoagent-CSRF"] = config["csrf"]
            headers.update(extra)
            conn = http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            try:
                conn.request("POST","/api/sessions",body="{}",headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status,403)
                response.read()
            finally:
                conn.close()
        client.request("POST","/api/sessions",body="{}",headers={"Cookie":cookie,"Content-Type":"application/json","X-Mawoagent-CSRF":config["csrf"]})
        response = client.getresponse()
        self.assertEqual(response.status,200)
        sid = json.loads(response.read())["id"]
        client.request("GET","/api/events?session="+sid,headers={"Cookie":cookie})
        response = client.getresponse()
        self.assertEqual(response.status,200)
        self.assertEqual(response.getheader("Content-Type"),"text/event-stream; charset=utf-8")
        self.assertEqual(response.readline(),b'event: state\n')
        self.assertIn(sid.encode(),response.readline())


if __name__ == "__main__":
    unittest.main()
