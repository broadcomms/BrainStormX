/* transcription-worklet.js
 * AudioWorkletProcessor that converts Float32 audio to 16-bit PCM frames
 * and posts raw PCM Int16Array buffers back to the main thread.
 *
 * Configurable parameters via processor options:
 *  - sampleRate: (number) expected sample rate (default from context)
 *  - frameSize: (number) number of samples per emitted frame (default 640 = 40ms @16kHz)
 *
 * The processor accumulates input until frameSize reached, then flushes.
 */
class TranscriptionPCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.sampleRate = globalThis.sampleRate; // context sample rate
    this.targetRate = opts.sampleRate || 16000;
    this.frameSize = opts.frameSize || 640; // 40ms @16kHz
    // Raw input (context rate) accumulation buffer
    this._buffer = new Float32Array(0);
    // Factor for simple decimation
    this._seq = 0;
    this._downsample = (this.sampleRate !== this.targetRate);
    this._dsFactor = this._downsample ? (this.sampleRate / this.targetRate) : 1;
    // For bookkeeping of dropped remainder when downsampling
    this._consumedInputFrames = 0;
  }

  _append(input) {
    if (!this._buffer.length) {
      this._buffer = input.slice();
    } else {
      const merged = new Float32Array(this._buffer.length + input.length);
      merged.set(this._buffer, 0);
      merged.set(input, this._buffer.length);
      this._buffer = merged;
    }
  }

  _consumeFrames() {
    const frames = [];
    if (!this._downsample) {
      while (this._buffer.length >= this.frameSize) {
        frames.push(this._buffer.slice(0, this.frameSize));
        this._buffer = this._buffer.slice(this.frameSize);
      }
      return frames;
    }
    // Decimation strategy: need frameSize * dsFactor input samples to form one output frame.
    const neededOriginal = Math.floor(this.frameSize * this._dsFactor);
    if (neededOriginal <= 0) return frames;
    while (this._buffer.length >= neededOriginal) {
      const out = new Float32Array(this.frameSize);
      for (let i = 0; i < this.frameSize; i++) {
        const srcIndex = Math.floor(i * this._dsFactor);
        out[i] = this._buffer[srcIndex];
      }
      frames.push(out);
      this._buffer = this._buffer.slice(neededOriginal);
    }
    return frames;
  }

  _floatToPCM16(floatBuf) {
    const out = new Int16Array(floatBuf.length);
    for (let i = 0; i < floatBuf.length; i++) {
      const s = Math.max(-1, Math.min(1, floatBuf[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return out;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) {
      return true;
    }
    const channelData = input[0];
    if (!channelData) return true;
    this._append(channelData);
    const frames = this._consumeFrames();
    for (const f of frames) {
      const pcm16 = this._floatToPCM16(f);
      // Transfer underlying ArrayBuffer to avoid copy
      this.port.postMessage({
        type: 'chunk',
        seq: this._seq++,
        pcm: pcm16.buffer,
        samples: f.length,
        sampleRate: this.targetRate,
      }, [pcm16.buffer]);
    }
    return true;
  }
}

registerProcessor('transcription-pcm-processor', TranscriptionPCMProcessor);
