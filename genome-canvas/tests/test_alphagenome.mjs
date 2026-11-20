import assert from "node:assert/strict";
import test from "node:test";
import { binsToFeatures, modelWindow } from "../alphagenome.mjs";

test("model windows: centred multiples of 2,048 bp, at most 524,288 bp; AG2 uses its three lengths", () => {
  const w = modelWindow("chr22", 36_200_000, 36_328_382);
  assert.equal((w.end - w.start) % 2048, 0);
  assert.ok(w.end - w.start >= 128_382 && w.end - w.start <= 524_288);
  assert.ok(Math.abs((w.start + w.end) / 2 - 36_264_191) <= 1);
  assert.equal(modelWindow("chr1", 1000, 1100).end - modelWindow("chr1", 1000, 1100).start, 2048);
  assert.equal(modelWindow("chr1", 0, 2_000_000).end - modelWindow("chr1", 0, 2_000_000).start, 524_288);
  assert.equal(modelWindow("chr1", 10_000, 20_000, "ag2").end - modelWindow("chr1", 10_000, 20_000, "ag2").start, 16_384);
  assert.equal(modelWindow("chr1", 0, 200_000, "ag2").end - modelWindow("chr1", 0, 200_000, "ag2").start, 524_288);
  assert.equal(modelWindow("chr1", 100, 200).start, 0);  // never before the chromosome's start
});

test("bins become contiguous wig features over the interval", () => {
  const f = binsToFeatures("chr2", 1000, 2000, [1, 2, 3, 4]);
  assert.equal(f.length, 4);
  assert.deepEqual(f[0], { chr: "chr2", start: 1000, end: 1250, value: 1 });
  assert.equal(f[3].end, 2000);
  for (let i = 1; i < f.length; i += 1) assert.equal(f[i].start, f[i - 1].end);
});
