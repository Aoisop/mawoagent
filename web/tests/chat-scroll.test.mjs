import test from 'node:test';
import assert from 'node:assert/strict';
import {ChatScrollController} from '../chat-scroll.mjs';

class Viewport {
  constructor() {
    this.clientHeight = 400;
    this.scrollHeight = 2000;
    this.top = 1600;
    this.handlers = {};
    this.blocks = [];
  }
  get scrollTop() {return this.top;}
  set scrollTop(value) {this.top = Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));}
  addEventListener(name, handler) {this.handlers[name] = handler;}
  getBoundingClientRect() {return {top:0};}
  querySelectorAll() {return this.blocks;}
  emit(name, event = {}) {this.handlers[name](event);}
  block(key, position, height = 300) {
    const block = {position,dataset:{chatKey:key},getBoundingClientRect:() => ({top:block.position-this.top,bottom:block.position-this.top+height})};
    this.blocks.push(block);
    return block;
  }
}

test('new chunks follow the end while the reader stays at the end', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  const before = controller.beforeUpdate();
  element.scrollHeight += 300;
  controller.restore(before);
  assert.equal(element.scrollTop,1900);
  assert.equal(controller.following,true);
});

test('scrolling up even 20 px pauses following before another chunk arrives', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  element.emit('wheel',{deltaY:-20});
  element.scrollTop -= 20;
  element.emit('scroll');
  const before = controller.beforeUpdate();
  element.scrollHeight += 300;
  controller.restore(before);
  assert.equal(element.scrollTop,1580);
  assert.equal(controller.following,false);
});

test('dragging the scrollbar to earlier content also pauses following', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  element.scrollTop = 800;
  element.emit('scroll');
  assert.equal(controller.following,false);
  element.scrollHeight += 600;
  controller.restore(controller.beforeUpdate());
  assert.equal(element.scrollTop,800);
});

test('a message growing above the viewport preserves the visible reading anchor', () => {
  const element = new Viewport();
  element.scrollTop = 800;
  const block = element.block('reading',750,800);
  const controller = new ChatScrollController(element);
  controller.pause();
  const before = controller.beforeUpdate();
  block.position += 200;
  element.scrollHeight += 200;
  controller.restore(before);
  assert.equal(block.getBoundingClientRect().top,-50);
  assert.equal(element.scrollTop,1000);
});

test('the latest button resumes following and is hidden after arriving', () => {
  const element = new Viewport();
  let state;
  const controller = new ChatScrollController(element,next => state=next);
  controller.pause();
  element.scrollTop = 900;
  element.emit('scroll');
  assert.equal(state.showLatest,true);
  controller.toLatest();
  element.emit('scroll');
  assert.equal(element.scrollTop,1600);
  assert.equal(state.showLatest,false);
  assert.equal(controller.following,true);
});

test('manually returning to the exact end resumes following', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  controller.pause();
  element.scrollTop = 900;
  element.emit('scroll');
  element.scrollTop = 1600;
  element.emit('scroll');
  assert.equal(controller.following,true);
});

test('switching sessions restores the previous reading position and intent', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  controller.activate('first');
  controller.restore(controller.beforeUpdate());
  element.scrollTop = 700;
  element.emit('scroll');
  controller.activate('second');
  controller.restore(controller.beforeUpdate());
  assert.equal(element.scrollTop,1600);
  controller.activate('first');
  controller.restore(controller.beforeUpdate());
  assert.equal(element.scrollTop,700);
  assert.equal(controller.following,false);
});

test('resizing a viewport retains history reading and pins the end only when following', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  controller.pause();
  element.scrollTop=800;
  element.emit('scroll');
  element.clientHeight=300;
  controller.resized();
  assert.equal(element.scrollTop,800);
  controller.toLatest();
  element.clientHeight=500;
  controller.resized();
  assert.equal(element.scrollTop,1500);
});

test('keyboard and touch scrolling upward both interrupt automatic following', () => {
  for (const input of ['keyboard','touch']) {
    const element = new Viewport();
    const controller = new ChatScrollController(element);
    if (input === 'keyboard') element.emit('keydown',{key:'PageUp',target:{}});
    else {
      element.emit('touchstart',{touches:[{clientY:100}]});
      element.emit('touchmove',{touches:[{clientY:130}]});
    }
    assert.equal(controller.following,false);
  }
});

test('hidden library pages do not discard the saved chat position', () => {
  const element = new Viewport();
  const controller = new ChatScrollController(element);
  controller.pause();
  element.scrollTop=600;
  element.emit('scroll');
  element.clientHeight=0;
  assert.equal(controller.beforeUpdate().top,600);
  controller.resized();
  element.clientHeight=400;
  controller.resized();
  assert.equal(element.scrollTop,600);
});
