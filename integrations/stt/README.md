# LEVI speech worker

Isolated speech-to-text worker for the marvel0 page's voice input. LEVI's service starts it as a subprocess on the first press of the microphone button and never imports Torch itself. The model is `Qwen/Qwen3-ASR-1.7B-hf` (Apache-2.0, bf16, transformers-native) behind a Silero VAD gate. The design record is `programs/rig-console/reviews/voice-design-20261020.md` in the levi-hub ledger (section 5.2 is this worker's contract).

```bash
./setup.sh                      # uv venv (torch 2.13.0 cu132) + model into ${LEVI_WORKSPACE:-<checkout>/.state}/checkpoints/stt
./setup.sh /data/stt            # other model folder
./setup.sh --from-dir SNAPSHOT  # copy a local snapshot instead of downloading
.venv/bin/python -m levi_stt_worker.cli --check                      # versions only, no model, no CUDA probe
.venv/bin/python -m levi_stt_worker.cli verify-model --model-dir D   # sha256 of every file against MANIFEST.sha256
```

The installer downloads one pinned commit into `<model-dir>/qwen3-asr-1.7b-hf/`, refuses weights whose sha256 differs from the pinned one, and writes `MANIFEST.sha256` (`sha256sum -c` format). It uses a scratch Hugging Face cache inside the model folder and removes it; the shared cache is never written. `qwen3-asr-0.6b-hf` (2.5 GB of GPU memory in the benchmark, not run through this worker) is the low-memory alternative.

## Protocol

`python -m levi_stt_worker.cli serve --model-dir D [--model qwen3-asr-1.7b-hf] [--device cuda] [--max-seconds 10] [--cpu-affinity 8-15] [--provider qwen3-asr|fake]`

stdin: one JSON line per request followed by exactly `bytes` raw bytes (a 16 kHz mono 16-bit WAV with a 44-byte header, 0.3 to `max-seconds` + 0.5 s):

```
{"op":"transcribe","id":"<request id>","bytes":N}\n<N bytes>
{"op":"shutdown"}\n
```

stdout, one JSON line per event:

| event | when |
|---|---|
| `{"event":"ready","model","device","load_s","vram_mib"}` | once, after the manifest check, the load and a warm-up; `device` is where the weights really sit (read from the model, not the option) |
| `{"event":"result","id","heard":true,"language","text","avg_logprob","speech_s","timing_ms":{"vad","infer"}}` | speech recognised |
| `{"event":"result","id","heard":false,"reason":"no_speech\|language\|low_confidence\|empty"}` | nothing usable |
| `{"event":"error","id","code":"audio_invalid\|too_long\|internal","message"}` | one request failed; the worker goes on |
| `{"event":"error","code":"manifest_mismatch\|model_missing\|internal"}` | startup failed, including a model that is not fully on the GPU or an error in the load or warm-up; exit 3 or 4 |

A bad WAV is answered and the next request is served. A malformed or oversized header and a short read cannot be recovered without guessing where the next request starts, so the worker answers once (`internal`, `too_long`, `audio_invalid`) and exits with code 2. EOF, `shutdown` and SIGTERM exit 0 and free the GPU. fd 1 is moved to stderr after the worker takes a private copy for events, so a library `print` cannot corrupt the protocol. stderr carries request id, byte count, a sha256 prefix and the outcome; never a transcript or a path.

## Gates

1. Silero VAD 6.2.3 on the CPU, threshold 0.35. Less than 0.25 s of speech in total: `no_speech`, the model is not called.
2. The model gets the detected speech with 1 s of audio on each side (see `gates.speech_audio` for why not the spans alone). Language is auto-detected, no prompt, no hotwords, greedy, 64 new tokens at most.
3. `language None` or empty text: `empty`. A language other than English or Chinese: `language`. Mean token log-probability of the text below -0.5, or generation stopped at the token limit: `low_confidence`.

A 6-talker babble and any very confident wrong reading still pass the gates, so every transcript needs a person's confirmation. Code-switched Chinese and English commands are unreliable.

## Fake provider

`--provider fake` needs only the standard library. Answers come from the JSON file named by `LEVI_STT_FAKE_SCRIPT`, one per request, in order. Entries: `{"heard":true,"language":"English","text":"...","avg_logprob":-0.1,"speech_s":1.8}`, `{"heard":false,"reason":"no_speech"}`, `{"error":{"code":"internal","message":"..."}}` or `{"crash":true}` (the process dies, exit 5); any entry may carry `"delay_s"`. The file may also be an object `{"answers":[...],"load_delay_s":0,"ready":{"load_s":1.2,"vram_mib":4700},"startup_error":{"code":"...","message":"..."}}`. The WAV and protocol checks stay real.

## Tests and measurements

`tests/stt/test_worker_units.py` and `test_worker_process.py` run with LEVI's own Python. `tests/stt/test_worker_real.py` needs `LEVI_STT_REAL_TEST=1`, this environment, the installed model and a GPU; run it through `driver.py run --resource gpu`.

Measured on the RTX 5090 (2026-10-20, synthetic speech): ready 4.6 s from process start (manifest hash 1.8 s, imports, load, warm-up), peak 4.7 GB GPU memory, 2 s command 100 to 115 ms, 3.3 s command 150 ms, silence or noise 13 ms (VAD only). Real voices and a real microphone are not yet verified.
