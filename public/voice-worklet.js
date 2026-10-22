// AudioWorklet of the marvel0's voice card (src/components/marvel0/voice-capture.ts).
// Served as a plain same-origin file rather than built from a Blob URL, so a
// page with a script-src policy still loads it. It hands the main thread
// 512-frame blocks of mono samples (the mean of the input channels) with each
// block's peak and RMS, which the page uses for the clip and for the level
// meter. It makes no network request and keeps no audio beyond one block.
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(512);
    this.filled = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0) return true;
    const frames = channels[0].length;
    for (let i = 0; i < frames; i += 1) {
      let sample = channels[0][i];
      // The node asks for one channel, so a browser normally mixes down
      // already; the mean makes a stereo input safe if it does not.
      for (let c = 1; c < channels.length; c += 1) sample += channels[c][i];
      this.buffer[this.filled] =
        channels.length > 1 ? sample / channels.length : sample;
      this.filled += 1;
      if (this.filled === this.buffer.length) this.flush();
    }
    return true;
  }

  flush() {
    const pcm = this.buffer;
    let peak = 0;
    let sum = 0;
    for (let i = 0; i < pcm.length; i += 1) {
      const a = Math.abs(pcm[i]);
      if (a > peak) peak = a;
      sum += pcm[i] * pcm[i];
    }
    this.port.postMessage({ pcm, peak, rms: Math.sqrt(sum / pcm.length) }, [
      pcm.buffer,
    ]);
    this.buffer = new Float32Array(512);
    this.filled = 0;
  }
}

registerProcessor("levi-pcm-tap", PcmTap);
