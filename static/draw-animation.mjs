// A presentation delay only: every snapshot already contains the committed server result.
export const DRAW_REVEAL_MS = 2400;

export function createDrawPresenter({
  render, start = () => {}, stop = () => {}, reveal = () => {},
  canAnimate = () => true, now = () => Date.now(),
  schedule = (fn, ms) => setTimeout(fn, ms), cancel = (id) => clearTimeout(id),
}) {
  let shown = null, pending = null, timer = null, generation = 0, patches = [];

  function clear() {
    generation += 1;
    if (timer !== null) cancel(timer);
    timer = null;
    const wasAnimating = pending !== null;
    pending = null;
    patches = [];
    if (wasAnimating) stop();
  }

  function finish(effects = true) {
    if (!pending) return;
    const next = pending;
    clear();
    shown = next;
    render(next);
    reveal(next, effects && canAnimate());
  }

  function update(next, {elapsedMs = 0} = {}) {
    // Polls, SSE notifications and the draw response can all report the same round.
    // Keep its newest marks/prize information without restarting the animation.
    if (pending && next.game.id === pending.game.id && next.draws.length === pending.draws.length) {
      for (const patch of patches) patch(next);
      pending = next;
      return;
    }
    clear();
    const previous = shown;
    const last = next.draws.at(-1);
    const oneNewDraw = previous && previous.game.id === next.game.id
      && next.draws.length === previous.draws.length + 1;
    // Use server time rather than the phone's clock, and conservatively include
    // the request round trip so slow replies shorten rather than extend the roll.
    const age = last ? ((next.server_time ?? now() / 1000) - last.drawn_at) * 1000 + elapsedMs : Infinity;
    const remainingMs = DRAW_REVEAL_MS - Math.max(0, Math.round(age));
    if (oneNewDraw && age >= -1000 && remainingMs > 150 && canAnimate()) {
      pending = next;
      const version = generation;
      const alreadyDrawn = new Set(previous.draws.map(d => d.number));
      const pool = Array.from({length: 49}, (_, i) => i + 1).filter(n => !alreadyDrawn.has(n));
      timer = schedule(() => { if (version === generation) finish(); }, remainingMs);
      start({round: last.round, remainingMs, pool});
    } else {
      shown = next;
      render(next);
      if (oneNewDraw) reveal(next, false);
    }
  }

  return {
    update,
    finish: () => finish(false),
    isAnimating: () => pending !== null,
    // Local writes can finish while a server snapshot waits to be revealed.
    patchPending: patch => { if (pending) { patches.push(patch); patch(pending); } },
    reset: () => { clear(); stop(); shown = null; },
  };
}
