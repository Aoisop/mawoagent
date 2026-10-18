/** Own the chat viewport; incoming tokens must not override reading intent. */
export class ChatScrollController {
  constructor(element, onChange = () => {}) {
    this.element = element;
    this.onChange = onChange;
    this.following = true;
    this.session = null;
    this.sessions = new Map();
    this.lastTop = element.scrollTop;
    this.programmaticTop = null;
    this.pending = null;
    this.snapshot = null;
    element.addEventListener('scroll', () => this.onScroll(), {passive:true});
    element.addEventListener('wheel', event => {
      if (event.deltaY < 0) this.pause();
    }, {passive:true});
    let touchY = null;
    element.addEventListener('touchstart', event => {touchY = event.touches[0]?.clientY;}, {passive:true});
    element.addEventListener('touchmove', event => {
      const next = event.touches[0]?.clientY;
      if (touchY !== null && next > touchY) this.pause();
      touchY = next;
    }, {passive:true});
    element.addEventListener('keydown', event => {
      if (event.target.closest?.('textarea,input,select')) return;
      if (['ArrowUp','PageUp','Home'].includes(event.key) || (event.key === ' ' && event.shiftKey)) this.pause();
    });
  }

  gap() {
    return Math.max(0, this.element.scrollHeight - this.element.clientHeight - this.element.scrollTop);
  }

  pause() {
    this.following = false;
    this.programmaticTop = null;
    this.snapshot = this.capture();
    this.notify();
  }

  onScroll() {
    const top = this.element.scrollTop;
    if (this.programmaticTop !== null && Math.abs(top - this.programmaticTop) <= 1) {
      this.programmaticTop = null;
    } else {
      if (top < this.lastTop - 1) this.following = false;
      // Resume only after a deliberate return to the actual end, not "near" it.
      if (this.gap() <= 2 && top > this.lastTop) this.following = true;
    }
    this.lastTop = top;
    this.snapshot = this.capture();
    this.notify();
  }

  capture() {
    if (!this.element.clientHeight && this.snapshot) return this.snapshot;
    const top = this.element.getBoundingClientRect().top;
    const visible = Array.from(this.element.querySelectorAll('[data-chat-key]')).find(node => node.getBoundingClientRect().bottom > top);
    return {following:this.following, top:this.element.scrollTop,
      anchor:visible ? {key:visible.dataset.chatKey, offset:visible.getBoundingClientRect().top - top} : null};
  }

  activate(id) {
    if (this.session === id) return;
    if (this.session) this.sessions.set(this.session, this.capture());
    this.session = id;
    this.pending = this.sessions.get(id) || {following:true, top:0, anchor:null};
    this.following = this.pending.following;
    this.programmaticTop = null;
  }

  beforeUpdate() {
    const snapshot = this.pending || this.capture();
    this.pending = null;
    return snapshot;
  }

  setTop(top) {
    this.element.scrollTop = top;
    this.lastTop = this.element.scrollTop;
    this.programmaticTop = this.lastTop;
  }

  restore(snapshot) {
    if (!snapshot || !this.element.clientHeight) return;
    this.following = snapshot.following;
    if (this.following) {
      this.setTop(this.element.scrollHeight);
    } else {
      const node = snapshot.anchor && Array.from(this.element.querySelectorAll('[data-chat-key]')).find(node => node.dataset.chatKey === snapshot.anchor.key);
      if (node) {
        const offset = node.getBoundingClientRect().top - this.element.getBoundingClientRect().top;
        this.setTop(this.element.scrollTop + offset - snapshot.anchor.offset);
      } else {
        this.setTop(snapshot.top);
      }
    }
    this.snapshot = this.capture();
    this.notify();
  }

  resized() {
    if (!this.element.clientHeight) return;
    this.restore(this.snapshot || this.capture());
  }

  toLatest() {
    this.following = true;
    this.setTop(this.element.scrollHeight);
    this.snapshot = this.capture();
    this.notify();
  }

  notify() {
    this.onChange({following:this.following, showLatest:!this.following && this.gap() > 2});
  }
}
