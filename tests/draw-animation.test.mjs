import assert from 'node:assert/strict';
import test from 'node:test';
import {createDrawPresenter, DRAW_REVEAL_MS} from '../static/draw-animation.mjs';

function snapshot(round = 0, {gameId = 1, age = 0, marks = [], winners = []} = {}) {
  return {
    game: {id: gameId}, server_time: 1000,
    draws: Array.from({length: round}, (_, i) => ({round: i + 1, number: i + 1, drawn_at: 1000 - age / 1000})),
    card: {marks}, winners,
  };
}

function harness() {
  let clock = 0, id = 0, animate = true;
  const timers = new Map(), rendered = [], started = [], revealed = [];
  const presenter = createDrawPresenter({
    render: s => rendered.push(s), start: s => started.push(s),
    reveal: (s, effects) => revealed.push({snapshot: s, effects}),
    canAnimate: () => animate,
    // Deliberately wrong client wall clock: server_time must determine freshness.
    now: () => 9999999999999,
    schedule: (fn, ms) => { timers.set(++id, {fn, at: clock + ms}); return id; },
    cancel: key => timers.delete(key),
  });
  return {
    presenter, rendered, started, revealed, timers,
    setMotion: value => { animate = value; },
    advance(ms) {
      clock += ms;
      for (const [key, timer] of [...timers]) if (timer.at <= clock) { timers.delete(key); timer.fn(); }
    },
  };
}

test('initial load, repeated state and an old single round display immediately', () => {
  const h = harness();
  h.presenter.update(snapshot(4));
  h.presenter.update(snapshot(4));
  h.presenter.update(snapshot(5, {age: 20000}));
  assert.equal(h.rendered.length, 3);
  assert.equal(h.started.length, 0);
  assert.equal(h.timers.size, 0);
});

test('one fresh round holds history and winners until the result is revealed once', () => {
  const h = harness(), before = snapshot(), after = snapshot(1, {winners: [{player_id: 1, round: 1}]});
  h.presenter.update(before);
  h.presenter.update(after);
  assert.equal(h.presenter.isAnimating(), true);
  assert.deepEqual(h.rendered, [before]);
  assert.equal(h.started[0].round, 1);
  h.advance(DRAW_REVEAL_MS - 1);
  assert.deepEqual(h.rendered, [before]);
  h.advance(1);
  assert.deepEqual(h.rendered, [before, after]);
  assert.equal(h.revealed.length, 1);
  assert.equal(h.revealed[0].effects, true);
  assert.equal(h.presenter.isAnimating(), false);
});

test('duplicate notifications keep the deadline and preserve the newest marks', () => {
  const h = harness();
  h.presenter.update(snapshot(3));
  h.presenter.update(snapshot(4));
  assert.ok(!h.started[0].pool.includes(1));
  const originalTimer = [...h.timers.keys()][0];
  h.advance(1000);
  const newest = snapshot(4, {marks: [0, 12], age: 1000});
  h.presenter.update(newest);
  assert.deepEqual([...h.timers.keys()], [originalTimer]);
  assert.equal(h.started.length, 1);
  h.advance(DRAW_REVEAL_MS - 1000);
  assert.equal(h.rendered.at(-1), newest);
  h.presenter.update(newest);
  assert.equal(h.started.length, 1);
  assert.equal(h.revealed.length, 1);
});

test('network latency shortens the roll using server time, independent of device clock', () => {
  const h = harness();
  h.presenter.update(snapshot());
  h.presenter.update(snapshot(1, {age: 900}));
  assert.equal(h.started[0].remainingMs, DRAW_REVEAL_MS - 900);
  h.advance(DRAW_REVEAL_MS - 900);
  assert.equal(h.rendered.at(-1).draws.length, 1);
  const slow = harness();
  slow.presenter.update(snapshot());
  slow.presenter.update(snapshot(1), {elapsedMs: 4000});
  assert.equal(slow.started.length, 0);
  assert.equal(slow.rendered.at(-1).draws.length, 1);
});

test('a manual mark saved during the roll is not replaced by its earlier snapshot', () => {
  const h = harness();
  h.presenter.update(snapshot()); h.presenter.update(snapshot(1));
  h.presenter.patchPending(s => { s.card.marks = [0, 12, 24]; });
  // A GET started before the save may still return its older marks afterwards.
  h.presenter.update(snapshot(1, {marks: []}));
  h.advance(DRAW_REVEAL_MS);
  assert.deepEqual(h.rendered.at(-1).card.marks, [0, 12, 24]);
  assert.equal(h.rendered.at(-1).draws[0].number, 1);
});

test('catch-up or a changed game cancels a pending roll and ignores its stale callback', () => {
  for (const replacement of [snapshot(3), snapshot(0, {gameId: 2})]) {
    const h = harness();
    h.presenter.update(snapshot()); h.presenter.update(snapshot(1));
    const staleCallback = [...h.timers.values()][0].fn;
    h.presenter.update(replacement);
    staleCallback(); h.advance(DRAW_REVEAL_MS);
    assert.equal(h.rendered.at(-1), replacement);
    assert.equal(h.presenter.isAnimating(), false);
    assert.equal(h.revealed.length, 0);
    assert.equal(h.timers.size, 0);
  }
});

test('reduced motion and background views show the committed result without scheduling', () => {
  const h = harness(); h.setMotion(false);
  h.presenter.update(snapshot()); h.presenter.update(snapshot(1));
  assert.equal(h.rendered.at(-1).draws.length, 1);
  assert.equal(h.started.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.revealed[0].effects, false);
});

test('hiding the page mid-roll reveals immediately; reset cannot replay the old result', () => {
  const h = harness();
  h.presenter.update(snapshot()); h.presenter.update(snapshot(1));
  h.presenter.finish(); h.advance(DRAW_REVEAL_MS);
  assert.equal(h.rendered.at(-1).draws.length, 1);
  assert.equal(h.revealed.length, 1);
  assert.equal(h.revealed[0].effects, false);
  h.presenter.update(snapshot(2));
  h.presenter.reset(); h.advance(DRAW_REVEAL_MS);
  assert.equal(h.rendered.at(-1).draws.length, 1);
  h.presenter.update(snapshot(2));
  assert.equal(h.rendered.at(-1).draws.length, 2);
  assert.equal(h.started.length, 2);
  assert.equal(h.revealed.length, 1);
});
