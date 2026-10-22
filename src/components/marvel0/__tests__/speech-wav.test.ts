import { describe, expect, test } from "bun:test";
import {
  dbToFraction,
  downmix,
  encodeWav16,
  micErrorCode,
  resampleSinc,
  toDb,
} from "../voice-capture";

const bytesOf = async (blob: Blob) => new Uint8Array(await blob.arrayBuffer());

function tone(hz: number, rate: number, seconds: number, amplitude: number) {
  const out = new Float32Array(Math.round(rate * seconds));
  for (let i = 0; i < out.length; i += 1)
    out[i] = amplitude * Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
}
const sum = (...parts: Float32Array[]) => {
  const out = new Float32Array(parts[0].length);
  for (const part of parts)
    for (let i = 0; i < out.length; i += 1) out[i] += part[i];
  return out;
};

/** Amplitude of one frequency in x (Hann window, the first and last 0.1 s left
 * out so the filter's edges do not count). */
function amplitudeAt(x: Float32Array, rate: number, hz: number): number {
  const edge = Math.round(rate * 0.1);
  const window = x.subarray(edge, x.length - edge);
  const n = window.length;
  let re = 0;
  let im = 0;
  let weights = 0;
  for (let i = 0; i < n; i += 1) {
    const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
    re += w * window[i] * Math.cos((2 * Math.PI * hz * i) / rate);
    im -= w * window[i] * Math.sin((2 * Math.PI * hz * i) / rate);
    weights += w;
  }
  return (2 * Math.hypot(re, im)) / weights;
}
const dB = (ratio: number) => 20 * Math.log10(ratio);

describe("the WAV the server accepts", () => {
  test("is the 44-byte header of design record 5.5 and the samples, byte for byte", async () => {
    const wav = encodeWav16(Int16Array.of(1, -2, 32767));
    expect(wav.type).toBe("audio/wav");
    const expected = [
      // "RIFF", 36 + 6 data bytes
      0x52, 0x49, 0x46, 0x46, 42, 0, 0, 0,
      // "WAVE", "fmt ", 16
      0x57, 0x41, 0x56, 0x45, 0x66, 0x6d, 0x74, 0x20, 16, 0, 0, 0,
      // PCM, 1 channel, 16000 Hz, 32000 bytes/s, 2 bytes/frame, 16 bits
      1, 0, 1, 0, 0x80, 0x3e, 0, 0, 0x00, 0x7d, 0, 0, 2, 0, 16, 0,
      // "data", 6
      0x64, 0x61, 0x74, 0x61, 6, 0, 0, 0,
      // the samples, little endian
      0x01, 0x00, 0xfe, 0xff, 0xff, 0x7f,
    ];
    expect(Array.from(await bytesOf(wav))).toEqual(expected);
  });

  test("has exactly 44 + 2 bytes per sample, with the data length equal to what follows", async () => {
    const samples = new Int16Array(16000 * 3);
    const bytes = await bytesOf(encodeWav16(samples));
    const view = new DataView(bytes.buffer);
    expect(bytes.length).toBe(44 + samples.length * 2);
    expect(view.getUint32(4, true)).toBe(bytes.length - 8);
    expect(view.getUint32(40, true)).toBe(bytes.length - 44);
  });
});

describe("resampleSinc", () => {
  const rate = 48000;
  const mixed = sum(tone(1000, rate, 1.5, 0.4), tone(11000, rate, 1.5, 0.2));

  test("keeps an 11 kHz tone from folding to 5 kHz: below -40 dB of the 1 kHz tone", () => {
    const out = resampleSinc(mixed, rate, 16000);
    const wanted = amplitudeAt(out, 16000, 1000);
    const alias = amplitudeAt(out, 16000, 5000);
    expect(dB(wanted / 0.4)).toBeGreaterThan(-1);
    expect(dB(wanted / 0.4)).toBeLessThan(1);
    expect(dB(alias / wanted)).toBeLessThan(-40);
  });

  test("the measurement sees the alias that naive decimation makes (so the test can fail)", () => {
    const naive = new Float32Array(Math.floor(mixed.length / 3));
    for (let i = 0; i < naive.length; i += 1) naive[i] = mixed[i * 3];
    const wanted = amplitudeAt(naive, 16000, 1000);
    const alias = amplitudeAt(naive, 16000, 5000);
    expect(dB(alias / wanted)).toBeGreaterThan(-10);
  });

  test("does the same from 44.1 kHz, and keeps the length by the ratio", () => {
    const source = sum(
      tone(1000, 44100, 1.5, 0.4),
      tone(11000, 44100, 1.5, 0.2),
    );
    const out = resampleSinc(source, 44100, 16000);
    expect(out.length).toBe(Math.floor((source.length * 16000) / 44100));
    const wanted = amplitudeAt(out, 16000, 1000);
    expect(dB(amplitudeAt(out, 16000, 5000) / wanted)).toBeLessThan(-40);
  });

  test("returns the samples as they are when the rates are equal", () => {
    const x = tone(440, 16000, 0.1, 0.5);
    expect(resampleSinc(x, 16000, 16000)).toBe(x);
  });
});

describe("downmix", () => {
  test("is the mean of the channels", () => {
    const out = downmix([
      Float32Array.of(1, 0, 0.5),
      Float32Array.of(0, 1, 0.5),
    ]);
    expect(Array.from(out)).toEqual([0.5, 0.5, 0.5]);
  });
  test("copies a single channel", () => {
    const left = Float32Array.of(0.25, -0.25);
    const out = downmix([left]);
    expect(Array.from(out)).toEqual([0.25, -0.25]);
    expect(out).not.toBe(left);
  });
});

describe("levels", () => {
  test("silence reads the floor and a half-scale sine about -9 dB", () => {
    expect(toDb(0)).toBe(-100);
    expect(toDb(0.5 / Math.SQRT2)).toBeCloseTo(-9.03, 1);
  });
  test("the bar is empty at -60 dB and full at 0 dB", () => {
    expect(dbToFraction(-100)).toBe(0);
    expect(dbToFraction(-60)).toBe(0);
    expect(dbToFraction(-30)).toBeCloseTo(0.5, 5);
    expect(dbToFraction(0)).toBe(1);
    expect(dbToFraction(6)).toBe(1);
  });
});

describe("micErrorCode", () => {
  const named = (name: string) => Object.assign(new Error(name), { name });
  test("tells a refusal, a missing device and a busy one apart", () => {
    expect(micErrorCode(named("NotAllowedError"))).toBe("denied");
    expect(micErrorCode(named("SecurityError"))).toBe("denied");
    expect(micErrorCode(named("NotFoundError"))).toBe("no_device");
    expect(micErrorCode(named("NotReadableError"))).toBe("busy");
    expect(micErrorCode(named("AbortError"))).toBe("busy");
    expect(micErrorCode(named("TypeError"))).toBe("unsupported");
    expect(micErrorCode(named("SomethingElse"))).toBe("error");
    expect(micErrorCode(null)).toBe("error");
  });
});
