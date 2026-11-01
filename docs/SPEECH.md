# Speech input for the marvel0 page / marvel0 页面的语音输入

The demo client command card of the **marvel0** page can take the task instruction by voice. You hold a button and say the instruction; a speech model on this machine turns it into text; LEVI finds the closest of the **allowed** task instructions; and when you click **Use**, that sentence from the list is put into the task instruction field. Beside it the card always offers [the sentence as you said it](#the-sentence-as-heard--听到的这句), tidied, with a **Use** of its own. From there it is what typing would have done: you click **Make command**, copy it, and press Enter in the client terminal yourself. Nothing here starts the client or the arm, unless you switch on [voice run](#voice-run--语音直跑).

marvel0 页面的评测客户端命令卡片可以用语音填写任务指令。按住按钮说出指令，本机上的语音模型把它转成文字，LEVI 在**允许的**任务指令里找最接近的一句；你点击“使用”之后，填进任务指令框的是列表里的那句原文。卡片旁边总会多给一个[按你说的话整理的那一句](#the-sentence-as-heard--听到的这句)，有它自己的“使用”。之后和手打一样：你点“生成命令”、复制，再自己在客户端终端按 Enter。除非你打开[语音直跑](#voice-run--语音直跑)，这里没有任何一步会启动客户端或机械臂。

## What it does, and never does / 做什么，绝不做什么

It does:

- record while a button is held (at most 10 seconds) as 16 kHz mono 16-bit WAV, in the browser, from the microphone chosen on the card (the browser's default unless another one is chosen, see below); the page must be opened as `http://127.0.0.1:7860` or `http://localhost:7860` (a browser gives the microphone to no other address);
- transcribe it with `Qwen3-ASR-1.7B` (Apache-2.0) in a **separate worker process** on this machine's GPU, offline, started on the first recording and gone after two minutes without one;
- compare the transcript with the allowed instructions (below) and offer the closest: tier **A** pre-selects one, **B** offers the best three, **C** lists all of them, **V** (a stop, negation or unsafe word) offers none;
- always offer the sentence as heard (tidied) as one more option beside those, never as the default;
- show how long each answer took and where the time went (see [Where the time goes](#where-the-time-goes--时间花在哪));
- change the task instruction field only when you click **Use**.

It never:

- starts, stops or connects to the policy server, the client or the arm stack, presses Enter, or sends a motion command; the sequence **Use**, **Make command**, **Copy** is three separate clicks of yours. The one exception is [voice run](#voice-run--语音直跑), which you switch on yourself: it makes the command, starts the hosted client and presses its start gate's key, and still never stops anything or touches the policy server;
- stops or pauses the robot. There is no voice stop and nothing on the page listens for one. **Speech cannot stop the arm: use the physical e-stop.** The page says so next to the card, permanently;
- changes any other field (`--min-z`, RTC, steps, episode count, rollout group, note, the confirmation phrases of the deploy card, the automatic-evaluation wizard);
- puts the transcript into a command by itself. What can reach the field is a sentence of the allowed list, the sentence LEVI generated in the datasets' most common phrasing, shown as generated (see [Generated sentences](#generated-sentences--生成句)), or, only when you press its own **Use**, [the sentence as you said it](#the-sentence-as-heard--听到的这句) (the transcript tidied up, with the same check as the field, and never for a restricted model); no sentence is used unless you press a button for it, except what the [voice run](#voice-run--语音直跑) runs after its countdown, and that is never the sentence as heard;
- listens continuously, waits for a wake word, or keeps the microphone open between recordings;
- keeps audio or text. The audio exists in memory and in the worker's pipe; the transcript only in the answer to the page (and in the page's memory for 60 seconds). Neither goes to a file, a log line, an error message or a URL. The log gets the request id, an outcome word, byte counts, the timings in milliseconds and eight characters of a SHA-256;
- sends anything off the machine: the model runs offline (`HF_HUB_OFFLINE=1`), and no cloud recogniser and no language model is involved;
- takes the physical GPU lock or uses `levi/inference/gpu.py` (either would stop a policy deploy or refuse exactly while the policy server runs).

它会做的：

- 按住按钮时在浏览器里录音（最长 10 秒），格式是 16 kHz 单声道 16 位 WAV，用的是卡片上选定的麦克风（没有另选时是浏览器的默认麦克风，见下）；页面必须以 `http://127.0.0.1:7860` 或 `http://localhost:7860` 打开（浏览器不会把麦克风给任何其他地址）；
- 用 `Qwen3-ASR-1.7B`（Apache-2.0）在本机 GPU 上的**独立工作进程**里转写，离线运行；第一次录音时启动，两分钟没有新录音就退出；
- 把转写文字和允许的指令（见下）比较并给出最接近的：A 档预选一句，B 档给出最好的三句，C 档列出全部，V 档（出现“停”、否定或不安全的词）不给候选；
- 总是把按听到的话整理出的那一句作为多一个选项给出，永远不是默认项；
- 在每个回答下显示用了多久、时间花在哪（见[时间花在哪](#where-the-time-goes--时间花在哪)）；
- 只有你点击“使用”时才改任务指令框。

它绝不做的：

- 启动、停止或连接策略服务器、客户端或机械臂栈，按 Enter，或发运动命令；“使用”“生成命令”“复制”是你的三次独立点击。唯一的例外是你自己打开的[语音直跑](#voice-run--语音直跑)：它会生成命令、启动托管客户端并按开始闸门的键，但仍然不停止任何东西，也不碰策略服务器；
- 让机器人停下或暂停。没有语音停机，页面上也没有任何东西在听停止词。**语音不能让机械臂停下，请按物理急停。** 页面在卡片旁永久写着这句话；
- 改任何其他字段（`--min-z`、RTC、步数、片段数、rollout 组、备注、部署卡片的确认短语、自动评测向导）；
- 自己把转写文字放进命令。能进入输入框的只有：允许列表里的句子；LEVI 按数据集最常见句式生成、并标明是生成句的那一句（见[生成句](#generated-sentences--生成句)）；或者只有你按它自己的“使用”时，[按你说的话整理的那一句](#the-sentence-as-heard--听到的这句)（整理过的转写文字，经过和输入框相同的检查，受限模型永远不行）。没有哪一句不按按钮就被使用，只有[语音直跑](#voice-run--语音直跑)倒计时后运行的那一句例外，而它从来不是“听到的这一句”；
- 持续监听、等唤醒词，或在两次录音之间保持麦克风打开；
- 保存音频或文字。音频只存在于内存和工作进程的管道里；转写文字只存在于给页面的回答里（以及页面内存里 60 秒）。两者都不写入文件、日志、错误信息或 URL。日志只有请求 ID、一个结果词、字节数、各段耗时（毫秒）和 SHA-256 的前八个字符；
- 把任何东西发到本机之外：模型离线运行（`HF_HUB_OFFLINE=1`），不涉及云端识别，也不涉及语言模型；
- 取物理 GPU 锁或使用 `levi/inference/gpu.py`（前者会挡住策略服务器部署，后者恰好在策略服务器运行时拒绝）。

## Voice run / 语音直跑

The voice card has a **Switch voice run on** button (owner request 2026-11-01). It opens a confirm window with the three ticks of the start window (a hand on the physical e-stop, the scene reset, the area clear); after **Switch on**, a spoken instruction runs without another confirmation until you press **Switch voice run off** or reload the page (the switch is not remembered).

- **Which answer runs by itself**: one clear sentence of an answer shown after the switch was turned on, used 2 s after it is shown unless you press **Cancel** (which also revokes the use while it is being checked), open the other choices or edit the text: a tier-A instruction, or the generated sentence (its dataset original when there is one). The generated sentence also runs for a tier-B or C answer on purpose: B and C mean no allowed instruction says what was heard, while the generated sentence was read back to hold exactly the heard objects with their colours and sizes. When one of its object names was translated by the local model (and passed the check), it runs after 4 s instead of 2 s, and the countdown shows the translation ("金桔 → kumquat"): the policy was never trained on that name and this path has no measured error rate, so there is time to read it and press **Cancel** (owner 2026-11-01: run it rather than wait for a press). Words heard before the switch was turned on never run by themselves, even when a model switch shows them again. It is the same press as yours, with the same checks (on screen 800 ms, not expired, the allowed list unchanged). An answer with a stop, negation or unsafe word, edited text, a recording cut at 10 s, a guessed word, several candidates and no generated sentence, or a generated sentence that disagrees with the tier-A instruction does not run by itself; its buttons read **Use and run** / **Run selected** / **Run the generated sentence**, and your press runs it.
- **What never runs by itself**: [the sentence as heard](#the-sentence-as-heard--听到的这句). It has its own button (labelled **Run the sentence as heard** while the voice run is on) and the card says that it never starts by itself; the generated and matched sentences behave as above whether or not it is there
- **Where it goes**: with no client, the command is made from step 3's form with that sentence and the hosted client is started with your ticks; at the start gate with the same instruction, **Start next episode** is pressed; at the start gate with another instruction, the page's instruction change ends the client at the gate and starts it again with the new one. When the client the voice started reaches its first start gate with that instruction (within 120 s), the page presses **Start next episode** once: the arm goes home without any limit check, then the policy runs. It does not when the switch was turned off in the meantime, a hold was sent, another sentence came while the start was on its way, or the client is past its first episode (episodes this page did not see). Switching off while a start or change is on its way may still bring the client up to its gate, where nothing moves; the card says so. While an episode runs, waits for its label, or a client is starting, the sentence is refused and not queued, and a refused sentence also drops the Enter an earlier one was waiting for. At the gate, another instruction for a client of another model than step 3's is refused (the change restarts the client with its saved model).
- **What stays**: the push-to-talk button is still the only way in (speech near the page does nothing unless a person holds it); every check of the backend applies (the start's preflight: policy server ready and matching, monitor green, arm lock free, and so on; the one-time challenge of each key); voice still cannot stop or pause the arm. A frozen or lost client switches voice run off (and it cannot be switched on while the client is frozen or lost), so the ticks are given again for the next scene; so does a page state that hides the voice card (step 3 filled from a failed instruction change, a site that cannot be read).
- The rules are in `src/components/marvel0/voice-run.ts`, the page's state in `use-voice-run.ts`; tests: `voice-run.test.ts` and `voice-run-flow.test.tsx` (with the cases of the 2026-11-01 independent review). Not yet tried on the real arm.

语音卡片上有一个“开启语音直跑”按钮（用户 2026-11-01 的要求）。它会打开一个确认窗，勾选和“启动客户端”相同的三项（手放在实体急停上、场景已复位、区域已清空）；点“开启”之后，说出的指令不再逐步确认，直到你点“关闭语音直跑”或刷新页面（开关不会被记住）。

- **哪些结果会自动运行**：开启之后出现的结果里唯一明确的一句，显示 2 秒后自动使用；期间点“取消”（正在检查的那次使用也会撤回）、打开其他候选或改文字，都不会运行。可以自动运行的是 A 档指令，或生成句（有数据集原句时用原句）。B 档、C 档结果的生成句也会自动运行，这是有意的：B、C 表示允许列表里没有一句说的是听到的内容，而生成句回读后正好包含听到的物体及其颜色和大小。如果其中有本地模型翻译（并通过检查）的物体名，倒计时从 2 秒延长到 4 秒，并显示译文（“金桔 → kumquat”）：策略没训练过这个名字，这条路径也没有实测误差，所以留出时间看清并点“取消”（用户 2026-11-01：照样自动运行，不必等人点）。开启之前听到的话不会自动运行，换模型后重新显示也一样。这和你自己点是同一个操作，检查也一样（显示满 800 毫秒、没过期、允许列表没变）。带停止、否定或不安全词的结果、改过的文字、被 10 秒截断的录音、有猜测的词、有几条候选又没有生成句、生成句和 A 档指令不一致，这些都不会自动运行；这时按钮显示“使用并运行”“运行所选”“运行生成句”，你点哪条就运行哪条。
- **绝不自动运行的**：[听到的这一句](#the-sentence-as-heard--听到的这句)。它有自己的按钮（语音直跑开着时叫“就运行听到的这句”），卡片上写明它不会自己开始；有没有它，生成句和匹配句的行为都和上面一样
- **去哪里**：没有客户端时，用第 3 步的表单加这句指令生成命令，并带着你的勾选启动托管客户端；客户端在开始闸门且指令相同时，直接按“开始下一个片段”；在开始闸门但指令不同时，用页面已有的换指令流程先在闸门处结束客户端，再用新指令重启。语音启动的客户端带着这句指令到达第一个开始闸门后（120 秒内），页面会按一次“开始下一个片段”：手臂先不受任何限制地回位，然后策略开始运行。期间关闭了语音直跑、发出了“保持”、启动途中又说了另一句，或客户端已经过了第一个片段（本页没看到的片段），都不会按。启动或换指令途中关闭开关，客户端仍可能被启动到开始闸门，那里不会动；卡片会说明。片段运行中、等待判定或客户端正在启动时，这句话会被拒绝，不会排队；被拒绝的新指令也会让之前那条不再自动开始。在开始闸门换指令时，如果客户端的模型和第 3 步选的不同，会被拒绝（换指令会用客户端保存的模型重启）。
- **不变的**：按住说话按钮仍是唯一入口（没人按住时，页面附近说话没有任何作用）；后端的所有检查照旧（启动前检查：策略服务器就绪且匹配、健康监视器绿灯、手臂锁空闲等；每个按键的一次性确认令牌）；语音仍然不能让手臂停下或暂停。客户端被冻结或失联时语音直跑会自动关闭（冻结或失联期间也打不开），下一次要重新勾选；页面状态让语音卡片隐藏时（第 3 步按换指令失败回填、读不到站点配置）也会关闭。
- 规则在 `src/components/marvel0/voice-run.ts`，页面状态在 `use-voice-run.ts`；测试是 `voice-run.test.ts` 和 `voice-run-flow.test.tsx`（含 2026-11-01 独立审查的复现用例）。还没在真机上试过。

## Choosing the microphone / 选择麦克风

A workstation often has several inputs: a dedicated USB microphone, a camera's microphone, the board's jack. The default input is not always the one the person talks into, so the card has a **Microphone** list above the talk button.

- The list holds **Default (the browser picks)** and the connected audio inputs. The default is whatever the browser opens when no input is named: Firefox uses the system's default input (its permission prompt may also let the person pick one), Chrome its own microphone setting (the camera icon in the address bar, or `chrome://settings/content/microphone`), which may differ from the system's. The list is read with the browser's `enumerateDevices`, which opens no microphone and asks nothing; it is read again when an input is plugged in or out and after the microphone was opened. Until this page has been allowed to use the microphone once, browsers keep the names back: the list then holds only the default and says to press the talk button once.
- A choice is remembered in this browser only (`localStorage`, the input's id and name). It is not sent to LEVI and is not shared with other browsers or people. Where the browser's storage cannot be written (blocked site data), the choice still applies on the page but is not remembered after a reload.
- A press opens the chosen input with `deviceId: {exact}`, so the browser cannot quietly substitute another one. Changing the choice releases a microphone that is still kept open from the last recording; a recording in progress finishes on its input, and that input is released right after it.
- A remembered input that is not connected is kept: the card says it is missing and uses the default until it is plugged back in, and offers **Use the default from now on** to forget it. If the browser changed the input's id, the input with the same name is used when exactly one input has that name. If the chosen input vanishes between the list and the press, that recording uses the default and the card warns about it; that microphone is not kept open, so the next press tries the chosen input again.
- After each opening the card says which input was used ("Recording from: …"), so a camera microphone taken by mistake shows before the transcript is trusted. The notes are in one status region that screen readers announce.
- **Any microphone, no setup.** Whatever rate (44.1, 48, 96 kHz…) and number of channels an input delivers, the page mixes it to mono and resamples it to the 16 kHz the model takes, so a USB microphone works as soon as the system lists it. A recording whose loudest sample is below -30 dBFS is still sent, and the card says it was very quiet: move closer, or raise that microphone's input volume in the system sound settings (a new microphone often comes with a low one). Below -50 dBFS nothing is sent.
- **Swapping microphones.** The browser also remembers which inputs it has seen, by name (ids may change between sessions; the list holds names only). The first list it ever sees is only recorded. Later, an input never seen before is new:
  - the chosen input is not connected and exactly one new input appeared: that is a swap, and the card switches to the new one, remembers it and says so, with a button to use the default instead. A new input that is still offered counts too, so plugging the new microphone in before unplugging the old one also switches. The card keeps the person's own choice and goes back to it as soon as it is connected again (also while the new one is still plugged in); any choice the person makes ends that;
  - otherwise (the chosen one is still connected, nothing was chosen, or several are new) the card only offers each new input ("Use …", or "Not now"); it never switches to one by itself, so a newly connected camera does not take over;
  - an input without a name is never treated as new, and neither is the input in use (a profile change can rename it). An offer goes when its input is unplugged, and is shown once: it is not repeated after a reload or after the input is plugged out and in again, whether or not it was dismissed.
- With the page open in several tabs, the first tab to see a change handles it; reload the others. The seen list holds the names of up to 64 inputs this browser has listed (on a shared account, other people's too) and never leaves the browser; clearing the site data of this address removes it, and the choice with it.

工作站上常有好几个输入设备：专用的 USB 麦克风、相机自带的麦克风、主板的接口。默认的输入设备不一定是人对着说话的那个，所以卡片在说话按钮上方有一个**麦克风**列表。

- 列表里是**默认（由浏览器决定）**和已连接的音频输入。“默认”指不指定设备时浏览器打开的那个：Firefox 用系统的默认输入（它的权限弹窗也可能让人另选一个），Chrome 用它自己的麦克风设置（地址栏里的摄像头图标，或 `chrome://settings/content/microphone`），可能和系统的不同。列表用浏览器的 `enumerateDevices` 读取，不会打开麦克风，也不会弹出询问；插拔设备时、以及麦克风打开过之后会重新读取。本页第一次获准使用麦克风之前，浏览器不给出设备名称：这时列表里只有默认，并提示先按一次说话按钮。
- 选择只记在这个浏览器里（`localStorage`，设备的 ID 和名称），不会发给 LEVI，也不会和别的浏览器或别人共享。浏览器存储写不进去时（网站数据被禁止），选择在当前页面仍然有效，但刷新后不会记住。
- 按下按钮时用 `deviceId: {exact}` 打开选定的设备，浏览器不能悄悄换成别的设备。改选设备会立即释放上一段录音后还保持打开的麦克风；正在进行的录音在原来的设备上录完，录完立即释放该设备。
- 记住的设备没有接上时，选择会保留：卡片提示它不在，在它重新插上之前使用默认麦克风，并提供“以后都用默认麦克风”按钮来忘掉它。浏览器换了设备 ID 时，如果正好只有一个设备叫同样的名称，就用那一个。选定的设备在列表读取之后、按下之前消失的，这一次录音使用默认麦克风，卡片会给出警告；这个麦克风不会保持打开，下一次按下会重新尝试选定的设备。
- 每次打开麦克风后，卡片显示用的是哪个设备（“录音用的麦克风：…”），所以误用了相机麦克风时，在采信转写之前就能看出来。这些提示放在同一个状态区域里，读屏软件会播报。
- **任何麦克风都不用设置。** 不管输入设备给出什么采样率（44.1、48、96 kHz……）和几个声道，页面都会混成单声道并重采样到模型要的 16 kHz，所以 USB 麦克风只要系统能识别就能用。录音最响的采样低于 -30 dBFS 时仍然发送，卡片会提示声音很小：请靠近，或在系统声音设置里调高这个麦克风的输入音量（新麦克风的默认音量常常偏低）。低于 -50 dBFS 时不发送。
- **更换麦克风。** 浏览器还会按名称记住见过哪些输入设备（设备 ID 可能每次会话都变；记录里只有名称）。第一次看到的列表只记录下来。之后，没见过的设备就是新的：
  - 选定的设备没有接上，并且正好出现了一个新设备：这就是更换，卡片自动改用新设备、记住它并提示，同时提供改用默认麦克风的按钮。还在提示中的新设备也算，所以先插新麦克风、再拔旧的，同样会切换。卡片会记住人原来的选择，一旦它重新接上就换回去（新设备还接着时也一样）；人自己做了任何选择后就不再换回；
  - 其他情况（选定的设备还接着、没有选过、或新设备不止一个）卡片只提示每个新设备（“改用 …”或“暂不使用”），不会自己切换，所以新接上的相机不会被自动选上；
  - 没有名称的设备永远不算新设备，正在使用的设备也不算（配置变化可能让它换个名字）。提示的设备被拔掉后提示消失；每个新设备只提示一次：不论有没有点过“暂不使用”，刷新页面或拔插之后都不再提示。
- 同时在几个标签页打开本页时，由最先看到变化的标签页处理；其他标签页请刷新。已见设备列表最多记 64 个本浏览器列出过的设备名称（共用账户时也包括别人的设备），不会离开浏览器；清除这个地址的网站数据会把它连同选择一起删掉。

## Which instructions are allowed / 哪些指令是允许的

A model of the restricted family (its name or serving config starts with `pi05_fr3_sp`) takes the two instructions it was trained on. Any other model takes the site file's `[prompts].known` list ([marvel0 site file](SETUP_WIZARD.md)). The page passes the model it is preparing a command for as `model_id`; without one the site list is used.

The matcher (`levi/marvel0/speech_match.py`, standard library only, no model) reads the transcript and each allowed instruction into the same structure (steps, actions, things with colour or size, order) in English and Chinese, and scores the differences. On 375 hand-written sentences and 1,611 generated near misses it never accepted a wrong instruction at tier A, which string-similarity measures (difflib, WRatio, sentence embeddings) could not achieve at any threshold. Two properties of the list make that hold, and `check_allowed_set` tests them on every request: every word of every allowed instruction is in the matcher's lexicon, and no two allowed instructions are close enough to be confused. When the check fails (for example after a new instruction was added to the site file) the answer is `speech_allowed_set_invalid` (412) and the worker is not touched; the fix is to add the reported words to `levi/marvel0/speech_lexicon.py`.

受限族模型（模型名或服务配置以 `pi05_fr3_sp` 开头）只接受它训练过的那两句。其他模型用站点文件 `[prompts].known` 的列表（见 [marvel0 站点文件](SETUP_WIZARD.zh-CN.md)）。页面把正在准备命令的模型作为 `model_id` 传来；没有传时用站点文件的列表。

匹配器（`levi/marvel0/speech_match.py`，只用标准库，没有模型）把转写文字和每一句允许的指令都按中英文读成同一种结构（步骤、动作、带颜色或大小的物体、顺序），再给差异打分。在 375 句手写句子和 1611 条程序生成的近似错句上，它从没有在 A 档放过一句错的指令；字符串相似度（difflib、WRatio、句向量）在任何阈值下都做不到。有两条性质保证这一点，`check_allowed_set` 在每次请求时检查：允许指令里的每个词都在匹配器的词表里，且任意两句允许指令之间不会近到混淆。检查不通过时（例如站点文件新加了一句指令）回答是 `speech_allowed_set_invalid`（412），工作进程不会被碰；处理办法是把报告的词加进 `levi/marvel0/speech_lexicon.py`。

## Generated sentences / 生成句

Owner decision 2026-10-30: besides the closest allowed instruction, the card offers a sentence **written** from what was said, in the phrasing the registered datasets use most, instead of only retrieving one. `levi/marvel0/speech_template.py` (standard library only) reads the action and the objects of the transcript, English or Chinese, through the matcher's lexicon, and puts them into a frame learned from the task instructions of every dataset in the catalog, weighted by episodes (a `Reset:` prefix is ignored, a namespace is not counted twice; the frames are read again when a dataset's revision changes):

- the verb is the one used most for that action, for example `pick` for placing, `stack`, `pour`;
- the preposition follows the destination as the datasets write it: `into` a bowl or drawer, `in` a basket, `on` a plate; an action keeps its own (`stack ... on` a cup even though water is poured `into` one);
- steps joined by "then" / 然后 become frames joined by `then`; a step without its verb keeps the previous action;
- an object outside the lexicon said in English is kept as said ("dragon fruit", one to three words), but only after a place word ("into", "on") when it is the destination, and never a direction or manner word ("left", "back", "quickly"); one said in Chinese gets its English from a local translation model, and only after a check (see "Translated object names" below); a Chinese name that holds a lexicon word (蓝莓, blueberry, holds 蓝, blue; 夹子, a clip, holds the verb 夹) is read as one new name; a size or a colour word with 色 before it stays a modifier (红色榴莲 is red + 榴莲); what cannot be told apart gives no sentence (`unclear`) instead of a guess: 文件夹放到 reads like 榴莲夹放到 ("clamp the durian and put it"), and 上衣 could be 上 + 衣; things joined by 和/跟/与/或/及 (two objects, or two colours of one) give no sentence (`unsupported`); a destination named before the object with 往/向/朝 gives none either (`no_task`; 往碗里放苹果: the frame would swap them), while 把苹果往碗里放 works, and neither does a place beside a thing (碗外头, 盘子旁边); a numbered known thing (一号盘子) is `unclear`;
- when one step names a specific action and a move ("pick up the cup and pour it into the bowl") the specific one is the task; two specific actions in one step give no sentence;
- a word no slot can hold (a number, a word in another script) gives no sentence (`unclear`), so nothing heard is dropped silently;
- the frames are learned from every instruction, reset instructions (`Reset: ...`) included, since they are the same robot's phrasing; the episode count shown includes them.

What keeps it safe: the block is labelled "generated: not an instruction the policy was trained on" and shows the frame and how many episodes use it; only its own **Use** press hands it over (or, with [voice run](#voice-run--语音直跑) switched on, its 2 s countdown), after the same 800 ms and freshness checks as any proposal; a dataset instruction with the same meaning is shown beside it exactly as the dataset wrote it, since the policy may know that wording better, only when every object is a lexicon word (the matcher's comparison skips a word it does not know, so "pencil sharpener" would have matched "pick pen ..."), and only a task instruction (never a `Reset:` one) that the matcher reads without a flag or an unknown word; a word the matcher guessed from a likely recognition mistake ("pearl" read as "pear") is listed as a guess (`repairs`, slot source `repair`); nothing is generated for a result with a stop, negation or unsafe flag, or for a model of the restricted family (both the server and the page enforce this); a frame that cannot hold the request ("same colour", "corresponding", "from ...", one object without a place) gives no sentence and says why; and the sentence is read back with the matcher's reader and must contain exactly the heard objects with their colours and sizes (a translated name counts as the one object it was heard as). Speech still never stops anything, and starts something only with [voice run](#voice-run--语音直跑) switched on. The answer field is `match.generated`: `status` (`ok`, `vetoed`, `restricted`, `no_task`, `unsupported`, `no_frame`, `untranslated`, `unclear`), `prompt`, `frames`, `episodes`, `slots` (`said`, `english`, `source`: `lexicon`, `repair`, `english`, `translation`), `untranslated`, `dataset_original`, `repairs` (`heard`, `as`), `translations` (below) and `untranslated_why`. A malformed row of a dataset is skipped and an unreadable dataset is left out; neither stops generation.

维护者 2026-10-30 决定：除了最接近的允许指令，卡片还给出一句按所说内容**生成**的句子，用已登记数据集里最常见的说法，而不是只检索一句已有的。`levi/marvel0/speech_template.py`（只用标准库）通过匹配器的词表读出转写里的动作和物体（中英文都可以），填进从目录里所有数据集的任务指令中学到的句式；统计按片段数加权（忽略 `Reset:` 前缀，命名空间不重复计数；数据集版本变化时重新读取）：

- 动词取这个动作最常用的，例如放置用 `pick`，还有 `stack`、`pour`；
- 介词跟着目标物体走，和数据集的写法一致：碗和抽屉用 `into`，篮子用 `in`，盘子用 `on`；动作有自己固定用法的保留自己的（杯子叠放用 `stack ... on`，即使倒水是 `into`）；
- 用 then / 然后 连接的几步，生成用 `then` 连接的几个句式；没说动词的一步沿用前一步的动作；
- 词表外的物体用英文说出时照原样保留（“dragon fruit”，一到三个词），但它作为目标时前面必须有介词（“into”“on”），而且不能是方向或方式词（“left”“back”“quickly”）；用中文说出的由本地翻译模型给出英文，并且先通过校验才使用（见下面的“翻译物体名”）；包含词表词的中文新名（蓝莓里有“蓝”，夹子里有动词“夹”）按一个新名字整体处理；前面的大小词和带“色”的颜色词仍是修饰（红色榴莲 = 红色 + 榴莲）；分辨不了的不猜，不生成（`unclear`）：“文件夹放到”和“榴莲夹放到”（夹起榴莲放下）读起来一样，“上衣”也可能是“上 + 衣”；用和/跟/与/或/及 连起来的（两样东西，或一样东西的两种颜色）不生成（`unsupported`）；用往/向/朝 把目标放在物体前面的也不生成（`no_task`；往碗里放苹果：句式会把两者对调），“把苹果往碗里放”可以；物体旁边的位置（碗外头、盘子旁边）也不生成；带编号的已知物体（一号盘子）是 `unclear`；
- 一步里同时有具体动作和“放”（“拿起杯子倒进碗里”）时，以具体动作为准；一步里有两个具体动作则不生成；
- 有放不进任何槽位的词（数字、其他文字的词）时不生成（`unclear`），听到的内容不会被悄悄丢掉；
- 句式从所有指令里学，包括复位指令（`Reset: ...`），因为它们是同一台机器人的说法；显示的片段数也包含它们。

安全措施：这一块标明“模板生成，不在训练过的指令里”，并显示句式和用它的片段数；只有点它自己的**使用**才会交出（打开[语音直跑](#voice-run--语音直跑)时也可以是它的 2 秒倒计时），和其他提案一样要过 800 毫秒和时效检查；数据集里意思相同的原句按数据集原样并排给出，因为策略可能更熟悉那个说法；只在所有物体都是词表词时给（匹配器比较时会跳过不认识的词，“pencil sharpener”会被当成“pick pen ...”），而且只给任务指令（不给 `Reset:` 指令）、匹配器读来没有标记也没有未知词的句子；匹配器根据常见识别错误猜出来的词（“pearl”读成“pear”）会作为猜测列出（`repairs`，槽位来源 `repair`）；结果带停止、否定或不安全标记时，以及受限族模型，都不生成（服务端和页面都检查）；句式装不下的请求（“相同颜色”“对应”“从……”、只有一个物体没有目标）不给句子并说明原因；生成的句子会用匹配器的读取器回读，必须正好包含听到的物体及其颜色和大小（译名按听到时的那一个物体算）。语音仍然不能停止任何东西；只有打开[语音直跑](#voice-run--语音直跑)时才会启动东西。回答字段是 `match.generated`：`status`（`ok`、`vetoed`、`restricted`、`no_task`、`unsupported`、`no_frame`、`untranslated`、`unclear`）、`prompt`、`frames`、`episodes`、`slots`（`said`、`english`、`source`：`lexicon`、`repair`、`english`、`translation`）、`untranslated`、`dataset_original`、`repairs`（`heard`、`as`）、`translations`（见下）和 `untranslated_why`。数据集里格式不对的行会跳过，读不了的数据集不计入，都不会让生成停下。

### Translated object names / 翻译物体名

When a Chinese object name is all that keeps a sentence from being generated (status `untranslated`), the server asks the text model of the **loaded** speech worker three short questions per name: the English name, that English translated back into Chinese, and whether the two name the same kind of object. `levi/marvel0/speech_translate.py` (standard library) then decides; a name is used only when:

1. the answer is one to three plain English words that name one object: not `NONE`; no action, step or stop phrase as the matcher reads it ("hold on", "followed by"); no place word or conjunction ("on", "and", "with"); not a direction, a manner, or a colour or size alone;
2. it is not an object the lexicon already knows (plurals included): the name was not in the lexicon, so an English that is a lexicon object (`orange` for 金桔, `pencil` for 蜡笔) is refused, and the card shows that object's Chinese names (`known_as`);
3. the back-translation is the same name (after traditional to simplified, 桔 = 橘, a final 子/儿/头 dropped); a back-translation that is only part of the name said is refused (牙刷架, a toothbrush holder, back as 牙刷: the translation dropped what made it another object); one that holds the whole name and more (乐高 back as 乐高积木), or that shares at least half of the shorter one's characters (花椰菜 and 花菜), passes only when the model also judges them the same object. Otherwise the translation is `unchecked` and refused.

A Chinese place word heard where a name should be (左边, 旁边, 上面, 这边 ...) is a place, not an object: the request is `unclear` and the word is never sent to the model, whose "left" would pass a back-translation check.

A used translation is shown under the sentence with its back-translation ("金桔 → kumquat (→ 金桔)"), and a refused one with the model's answer and the reason; the sentence is still only used by a person's **Use** press. The model is asked only when the worker is already loaded (a transcription loads it; `speech/match` never starts it), the guards above are open, and, the first time, the model's `TRANSLATE_VRAM_MIB` (4600 MiB for the default Qwen3.5-2B, 9500 MiB for the 4B) plus the site margin is free. Answers are kept in memory, 256 names, per model; nothing is written to disk or the log. `translations` lists one verdict per name: `said`, `english`, `back`, `check` (`back_translation`, `back_translation_and_judgement` or null), `ok`, `reason` (`not_object`, `known_as`, `unchecked` or null), `known_as`. `untranslated_why` says why the names were not asked at all: `off`, `not_installed`, `too_many` (more than 4), `blocked` (a guard), `not_loaded`, `busy`, `rate_limit` (10 a minute), `gpu_memory`, `no_time` (the page's wait would run out) or `failed`.

**Default model: Qwen3.5-2B** (since 2026-11-01; bf16, loaded text-only, 4.4 GB in the worker's process, about 0.66 s a name). With the production prompts, of the same 81 names 69 passed, all with a usable English (65 expected, 4 variants such as "clothing clip", none wrong); of the 40 held-out names 29 passed (one too narrow: "corkscrew" for 开瓶器; two clumsy but usable: "preservation box", "candy stick"); none of the 11 non-objects passed. Qwen3.5-0.8B (3.1 GB) translated only 50 of 81 correctly. The 4B below stays selectable (`LEVI_STT_TRANSLATE_MODEL=qwen3-4b-instruct-2507`).

Measured on 2026-10-31 (RTX 5090, Qwen3-4B-Instruct-2507 in bf16, greedy; record: levi-hub `programs/rig-console/reviews/voice-translate-20261031.md`): of 81 object names outside the lexicon, 69 passed the check, 64 with the expected English, 3 with a usable variant ("clothing clip", "badminton birdie", "wool ball") and 2 wrong ("honeydew" for 哈密瓜, "cup pad" for 杯垫); on a held-out set of 40 names, fixed before it was run, 31 passed and none was wrong (3 usable variants), and 4 wrong translations were caught (卷笔刀 → eraser, 沐浴露 → shampoo, 毽子 → jump rope, 漏勺 → sieve); none of 11 words that are not objects passed. Peak GPU memory 9,360 MiB, about 0.28 s per question, so about 0.9 s per name. Qwen3-1.7B passed 55 of 81 with 4 wrong; Qwen3-0.6B and the speech model itself translated too few names correctly. In a sentence ("把…放到蓝色碗里"), 117 of these 121 names reach the model whole; 小黄鸭 reaches it as small + 黄鸭; 衣夹 and 文件夹 before 放到 give no sentence (`unclear`, see above), and 螺丝刀 is read as two known objects (screw + knife) and gives none (`no_task`). The check rule above was tightened after two rounds of independent review on 2026-11-01 (parts, plurals, phrases); replaying the recorded answers gives the same counts.

当一句话只差中文物体名的英文时（状态 `untranslated`），服务端向**已加载的**语音工作进程里的文本模型就每个名字问三个短问题：英文名、把这个英文再译回中文、两者是不是同一种东西。然后由 `levi/marvel0/speech_translate.py`（只用标准库）判断，只有满足下面几条才用：

1. 回答是一到三个普通英文单词，并且是一个物体名：不是 `NONE`；按匹配器读来没有动作、分步或停止说法（“hold on”“followed by”）；没有介词或连词（“on”“and”“with”）；不是方向、方式，也不只是颜色或大小；
2. 不是词表里已有的物体（复数也算）：这个名字不在词表里，所以译成词表物体的（金桔译成 `orange`、蜡笔译成 `pencil`）拒绝，卡片显示那个物体的中文叫法（`known_as`）；
3. 回译和原名相同（繁转简、桔 = 橘、去掉末尾的子/儿/头之后）；回译只是原名一部分的拒绝（牙刷架回译成牙刷：译文丢掉了让它成为另一种东西的那部分）；回译包含整个原名还多出内容的（乐高回译成乐高积木），或者两者至少共有较短那个一半的字（花椰菜和花菜），只有模型也判断是同一种东西才通过。否则译文是 `unchecked`，不用。

在该是物体名的位置听到中文方位词（左边、旁边、上面、这边……）时，它说的是位置不是物体：这句话是 `unclear`，这个词不会送给模型（模型译成 “left” 也能通过回译校验）。

用上的译文显示在句子下面，带回译（“金桔 → kumquat (→ 金桔)”）；没用上的显示模型的回答和原因；这句话仍然只有人点它的**使用**才会交出。只有工作进程已经加载时才会问模型（录音转写会加载它；`speech/match` 从不启动它），上面的守卫要放行，第一次加载还需要该模型的 `TRANSLATE_VRAM_MIB`（默认的 Qwen3.5-2B 是 4600 MiB，4B 是 9500 MiB）加站点余量的空闲显存。结果只存在内存里，最多 256 个名字，按模型区分；不写磁盘，也不写日志。`translations` 每个名字一条：`said`、`english`、`back`、`check`（`back_translation`、`back_translation_and_judgement` 或 null）、`ok`、`reason`（`not_object`、`known_as`、`unchecked` 或 null）、`known_as`。`untranslated_why` 说明这些名字为什么完全没问模型：`off`、`not_installed`、`too_many`（超过 4 个）、`blocked`（守卫）、`not_loaded`、`busy`、`rate_limit`（每分钟 10 次）、`gpu_memory`、`no_time`（页面的等待时间会用完）或 `failed`。

**默认模型：Qwen3.5-2B**（2026-11-01 起；bf16，只加载文字部分，工作进程里 4.4 GB，每个名字约 0.66 秒）。用线上提示词测同样的 81 个名字：69 个通过，英文都可用（65 个是预期的，4 个是“clothing clip”这类别称，没有错误）；40 个留出名字里 29 个通过（一个偏窄：开瓶器译成“corkscrew”；两个不地道但能用：“preservation box”“candy stick”）；11 个不是物体的词一个都没通过。Qwen3.5-0.8B（3.1 GB）81 个只译对 50 个。下面的 4B 仍可选（`LEVI_STT_TRANSLATE_MODEL=qwen3-4b-instruct-2507`）。

2026-10-31 实测（RTX 5090，Qwen3-4B-Instruct-2507，bf16，贪心解码；记录见 levi-hub `programs/rig-console/reviews/voice-translate-20261031.md`）：81 个词表外物体名里 69 个通过校验，其中 64 个是预期的英文，3 个是可用的别称（“clothing clip”“badminton birdie”“wool ball”），2 个错误（哈密瓜译成“honeydew”、杯垫译成“cup pad”）；运行前就固定好的 40 个留出名字里 31 个通过，没有错误（3 个可用别称），并拦下了 4 个错译（卷笔刀 → eraser、沐浴露 → shampoo、毽子 → jump rope、漏勺 → sieve）；11 个不是物体的词一个都没通过。显存峰值 9360 MiB，每个问题约 0.28 秒，每个名字约 0.9 秒。Qwen3-1.7B 81 个通过 55 个，错 4 个；Qwen3-0.6B 和语音模型本身译对的太少。放在句子里（“把…放到蓝色碗里”），这 121 个名字里有 117 个能整体送到模型；小黄鸭按“小 + 黄鸭”送；衣夹、文件夹在“放到”前面不生成句子（`unclear`，见上）；螺丝刀读成两个已知物体（螺丝 + 刀），也不生成（`no_task`）。上面的校验规则在 2026-11-01 两轮独立审查后收紧（部分回译、复数、短语），用录下的回答重放，计数不变。

## The sentence as heard / 听到的这句

Whatever the matcher and the generator make of it, **every answer with a transcript also offers the sentence as you said it** (owner request 2026-11-01: "whatever happens, at least offer the sentence I said"). It is the first block under the transcript, with its own button, **Use the sentence as heard**. It is one more option, not the default: the preselected instruction (tier A), the best three (B) and the generated sentence are as they were.

- **What it is**: the transcript (or the text you edited and matched again) with white space trimmed and folded, the sentence-ending punctuation (`. ! ? 。！？`) dropped and the letters lower-cased, because the datasets' instructions are lower case: "Pick tennis from white plate to green plate." becomes `pick tennis from white plate to green plate`. The server writes it (`match.as_heard.prompt`, `levi/marvel0/speech_api.py` `as_heard_sentence`); the page adds nothing.
- **The same check as the field**: the server runs the command card's own check on it (`command.prompt_problem`: at most 300 characters, no quote, backslash, `$`, backtick or `!`, no control character, no leading dash), and sends the reason in `match.as_heard.problem` (`null` when it passes). A sentence that fails is shown, disabled, with the field's own sentence for the reason; the rules are not repeated on the page.
- **Limits**: it is never used without your press of its button (800 ms after it appears, like any **Use**), and **never by the voice run**: with the voice run on, the button reads **Run the sentence as heard**, the card says it never starts by itself, and the countdown still belongs to the tier-A or generated sentence only. A restricted model (`family` `restricted`) takes only the instructions it was trained on, so the option is shown disabled with that reason whatever the server found. A result with a stop, negation or unsafe flag (tier **V**) has no candidates at all, this one included. Nothing of it reaches a log: the audit line has no text, only codes and numbers.
- **Chinese** is offered as it is, with a line saying that the policy was trained on English instructions and a Chinese sentence is likely to work poorly. Sentence translation is not built.
- Tests: `tests/marvel0/test_marvel0_speech_as_heard.py`; `speech-logic.test.ts`, `speech-session.test.tsx`, `speech-panel.test.tsx` and `voice-run.test.ts`.

不管匹配器和生成器怎么说，**只要有转写文字，回答里就总会多给一个“按你说的话原样整理的那一句”**（用户 2026-11-01 的要求：“不管怎么样至少要有说的这句话的指令”）。它是转写文字下面的第一块，有自己的按钮“就用听到的这句”。它只是多一个选项，不是默认项：预选的指令（A 档）、最好的三句（B 档）和生成句都和以前一样。

- **是什么**：转写文字（或你改过再匹配的文字），去掉首尾空白并折叠中间空白，去掉句末标点（`. ! ? 。！？`），字母转小写（数据集的指令都是小写）：“Pick tennis from white plate to green plate.”变成 `pick tennis from white plate to green plate`。由服务器生成（`match.as_heard.prompt`，`levi/marvel0/speech_api.py` 的 `as_heard_sentence`），页面不加任何东西。
- **和输入框同一个检查**：服务器用命令卡片自己的检查（`command.prompt_problem`：最多 300 个字符，不含引号、反斜杠、`$`、反引号和 `!`，不含控制字符，不以短横线开头）检查它，把原因放在 `match.as_heard.problem`（通过时是 `null`）。不通过的句子照样显示，但按钮禁用，并用输入框自己的措辞写明原因；页面上不重复这些规则。
- **限制**：不按它的按钮就绝不会使用（出现 800 毫秒后才能按，和其他“使用”一样），**语音直跑也绝不会自动运行它**：直跑开着时按钮叫“就运行听到的这句”，卡片上写明它不会自己开始，倒计时仍然只属于 A 档指令或生成句。受限模型（`family` 为 `restricted`）只接受训练过的指令，所以不管服务器检查出什么，这个选项都显示为禁用并写明这个原因。带“停”、否定或不安全标记的结果（V 档）完全没有候选，这一句也不例外。它不会进入任何日志：审计行没有文字，只有代码和数字。
- **中文**原样给出，并写明策略是用英文指令训练的、中文句子很可能效果不好。句子翻译没有做。
- 测试：`tests/marvel0/test_marvel0_speech_as_heard.py`；`speech-logic.test.ts`、`speech-session.test.tsx`、`speech-panel.test.tsx` 和 `voice-run.test.ts`。

## Where the time goes / 时间花在哪

Under every answer (also under "nothing heard" and under the answer to edited text) one muted line says how long it took and where, for example: `Took 4.8 s: model load 3.1 s, transcription 0.9 s, matching 0.1 s, rest (upload, page) 0.7 s`.

- **Total** is the page's own clock, from the release of the button (the end of the recording) to the answer; for edited text, from the press of **Match again**.
- **Model load** and **transcription** are the worker's own numbers (`timing_ms.load`, `timing_ms.infer`); the load is left out when it is 0 (the model was loaded already).
- **Matching** is measured around the matching and the generated sentence, including any object-name translation (`timing_ms.match`); it is left out when nothing was matched. **Rest** is the total minus the server's own total (`timing_ms.total`): the upload, the answer's way back and the page.
- The audit log line carries the same numbers and nothing else: `load_ms`, `infer_ms`, `match_ms`, `total_ms`. The idle unload (`IDLE_SECONDS`, 120 s) and the GPU settings are unchanged; these numbers are what to decide them from.

每个回答下面（“没听到”和改文字后的回答下面也一样）有一行灰色小字，说明用了多久、花在哪，例如：`用时 4.8 s：加载模型 3.1 s，转写 0.9 s，匹配 0.1 s，其余（上传、页面）0.7 s`。

- **总用时**是页面自己的计时，从松开按钮（录音结束）到回答；改文字的回答从点“再次匹配”算起。
- **加载模型**和**转写**是工作进程自己的数字（`timing_ms.load`、`timing_ms.infer`）；加载为 0（模型已经在显存里）时不显示。
- **匹配**量的是匹配加生成句的时间，包括物体名翻译（`timing_ms.match`）；没有匹配时不显示。**其余**是总用时减去服务器自己的总时间（`timing_ms.total`）：上传、回答回来的路和页面。
- 审计日志行带同样的数字，别的没有：`load_ms`、`infer_ms`、`match_ms`、`total_ms`。空闲卸载（`IDLE_SECONDS`，120 秒）和 GPU 设置没有改；要调它们，先看这些数字。

## When it refuses / 什么时候拒绝

Every transcription asks the policy deploy's passive status (`GET /api/levi/marvel0/deploy`, cached for two seconds, so no extra `nvidia-smi` or socket read) and refuses when:

| Reason             | Refusal                   | Status | Meaning                                                                                                                                                           |
| ------------------ | ------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `policy_starting`  | `speech_policy_starting`  | 409    | the policy server is `starting`, or its state is `unknown` (also when the status cannot be read)                                                                  |
| `client_connected` | `speech_client_connected` | 409    | a client is connected to the policy port: a run may be moving the arm, and the task instruction is the client's start parameter, so changing it now means nothing |
| `arm_in_use`       | `speech_arm_in_use`       | 409    | the AERI arm lock is held or cannot be read (the deploy's own check)                                                                                              |
| `gpu_insufficient` | `speech_gpu_insufficient` | 409    | the worker is not loaded and fewer than 5000 MiB plus the site's `gpu.margin_mib` are free                                                                        |
| `gpu_unreadable`   | `speech_gpu_unreadable`   | 503    | the worker is not loaded and the GPU's memory use cannot be read                                                                                                  |

A running arm stack alone is **not** a reason: it is the normal state during operation. Before a policy server is deployed, the deploy route releases the speech worker, so its roughly 4.9 GB never counts against the deploy's memory check.

每次转写都会读取策略部署的被动状态（`GET /api/levi/marvel0/deploy`，缓存两秒，所以不会多一次 `nvidia-smi` 或套接字读取），出现下面的情形就拒绝：

| 原因               | 拒绝码                    | 状态码 | 含义                                                                                             |
| ------------------ | ------------------------- | ------ | ------------------------------------------------------------------------------------------------ |
| `policy_starting`  | `speech_policy_starting`  | 409    | 策略服务器是 `starting`，或状态为 `unknown`（状态读不到时也算）                                  |
| `client_connected` | `speech_client_connected` | 409    | 有客户端连在策略端口上：可能正在运行并驱动机械臂，而任务指令是客户端的启动参数，此时修改没有意义 |
| `arm_in_use`       | `speech_arm_in_use`       | 409    | AERI 手臂锁被占用或读不到（部署自己的检查）                                                      |
| `gpu_insufficient` | `speech_gpu_insufficient` | 409    | 工作进程尚未加载，且空闲显存少于 5000 MiB 加站点文件的 `gpu.margin_mib`                          |
| `gpu_unreadable`   | `speech_gpu_unreadable`   | 503    | 工作进程尚未加载，且读不到 GPU 的显存占用                                                        |

只是机械臂栈在运行**不是**拒绝的理由：那是操作时的常态。部署策略服务器之前，部署路由会先释放语音工作进程，所以它约 4.9 GB 的显存不会计入部署的显存检查。

## Install / 安装

The worker has its own environment; LEVI's core never imports Torch.

```bash
integrations/stt/setup.sh      # uv venv, torch, the pinned Qwen3-ASR-1.7B-hf (4.1 GB) and MANIFEST.sha256
export LEVI_STT_WORKER_PYTHON=$PWD/integrations/stt/.venv/bin/python   # optional, this is the default
```

For translated object names, install the text model as well (`./setup.sh --model qwen3.5-2b`, 4.3 GB, a pinned commit of `Qwen/Qwen3.5-2B`, Apache-2.0; `--model qwen3-4b-instruct-2507` installs the 4B, 7.6 GB); without it the card says the names were not translated (`not_installed`).

`setup.sh` puts the model into `$LEVI_WORKSPACE/checkpoints/stt/qwen3-asr-1.7b-hf/` (never into the shared Hugging Face cache) and writes its `MANIFEST.sha256`; the worker checks every file against it each time it loads the model and refuses to start on a mismatch (`speech_manifest_mismatch`). More in [integrations/stt/README.md](../integrations/stt/README.md).

工作进程有自己的环境；LEVI 的核心从不导入 Torch。要翻译物体名，还要装文本模型（`./setup.sh --model qwen3.5-2b`，4.3 GB，`Qwen/Qwen3.5-2B` 的固定提交，Apache-2.0；`--model qwen3-4b-instruct-2507` 装 4B，7.6 GB）；没装时卡片会说明名字没有翻译（`not_installed`）。`setup.sh` 把模型放进 `$LEVI_WORKSPACE/checkpoints/stt/qwen3-asr-1.7b-hf/`（不写共享的 Hugging Face 缓存）并写出 `MANIFEST.sha256`；工作进程每次加载模型时都按它核对每个文件，不符就拒绝启动（`speech_manifest_mismatch`）。详见 [integrations/stt/README.md](../integrations/stt/README.md)。

## Settings / 设置

| Setting                  | Default                             |                                                                                                                                                                                                                                                     |
| ------------------------ | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LEVI_STT_WORKER_PYTHON` | `integrations/stt/.venv/bin/python` | the worker environment's Python / 工作进程环境的 Python                                                                                                                                                                                             |
| `LEVI_STT_MODEL_DIR`     | `$LEVI_WORKSPACE/checkpoints/stt`   | the folder of model folders (must be inside the workspace) / 存放模型文件夹的目录（必须在工作区内）                                                                                                                                                 |
| `LEVI_STT_MODEL`         | `qwen3-asr-1.7b-hf`                 | the model folder's name; `fake` runs the worker's `fake` provider (LEVI's own Python, no model, no GPU) for tests and demonstrations / 模型文件夹名；`fake` 运行工作进程的 `fake` 提供者（用 LEVI 自己的 Python，没有模型、不用 GPU），供测试和演示 |
| `LEVI_STT_FAKE_SCRIPT`   | unset                               | with `fake`: a JSON file of scripted answers, consumed in order (tests only) / 配合 `fake`：按顺序消费的脚本答案 JSON 文件（仅测试用）                                                                                                              |
| `LEVI_STT_TRANSLATE_MODEL` | `qwen3.5-2b`                      | the folder (in `LEVI_STT_MODEL_DIR`) of the text model that translates Chinese object names, `qwen3.5-2b` or `qwen3-4b-instruct-2507`; `off` turns translation off; a missing folder leaves it off / 翻译中文物体名的文本模型文件夹名（在 `LEVI_STT_MODEL_DIR` 里），`qwen3.5-2b` 或 `qwen3-4b-instruct-2507`；`off` 关闭翻译；文件夹不存在时也不翻译 |

Everything else is a constant: recordings of 0.3 to 10.5 s and at most 400,000 bytes; one transcription at a time and at most 10 a minute; idle exit after 120 s; 45 s to start and 20 s per transcription (the browser allows 75 s in total); free GPU memory of 5000 MiB plus the site margin for a cold start. Translation: at most 4 names of at most 16 characters a request and at most 10 requests a minute; 4600 MiB (Qwen3.5-2B; 9500 MiB for the 4B) plus the site margin free to load the text model (read fresh, not from the two-second status cache), and a request is begun only with 1 s a name left, plus 5.5 s for the first load (measured: 5.0 s for the 4B, 3.5 s of it the sha256 of 7.6 GB; about 2 s of hashing for the 2B's 4.3 GB); at most 20 s, cut to what is left of 68 s after a transcription and of 9.5 s after a match, so that stopping a worker that missed it (up to about 4.5 s) still ends before the page stops waiting (75 s and 15 s); with less than 3 s left nothing is asked. A translation model folder that fails its manifest or cannot be loaded is reported as not installed and not tried again until the worker restarts; the speech worker stays. The worker runs at `nice` 19, on the cores of the site file's `[unit] cpu_affinity` (the template's `8-15` is a guess nobody has confirmed), with 2 threads (`OMP_NUM_THREADS=2`) and `LEVI_GPU_LOCK_FILE` empty. Of LEVI's environment it receives only what it needs to find its libraries and the GPU: not the UI token, not Hugging Face credentials.

其余都是常量：录音 0.3 到 10.5 秒、不超过 400000 字节；同时只处理一次转写，每分钟最多 10 次；空闲 120 秒后退出；启动限时 45 秒，每次转写限时 20 秒（浏览器总期限为 75 秒）；冷启动需要 5000 MiB 加站点文件余量的空闲显存。翻译：每次最多 4 个名字、每个最多 16 个字，每分钟最多 10 次；加载文本模型需要 4600 MiB（Qwen3.5-2B；4B 是 9500 MiB）加站点余量的空闲显存（现读，不用两秒的状态缓存），每个名字至少要剩 1 秒才开始，第一次还要再加 5.5 秒用于加载（实测：4B 5.0 秒，其中 3.5 秒是 7.6 GB 的 sha256 核对；2B 的 4.3 GB 核对约 2 秒）；最多 20 秒，转写之后截到 68 秒里剩下的时间，匹配之后截到 9.5 秒里剩下的时间，这样超时停掉工作进程（最多约 4.5 秒）之后仍在页面放弃等待（75 秒和 15 秒）之前结束；剩下不到 3 秒就不问。翻译模型文件夹核对失败或加载不了时报“没有安装”，在工作进程重启前不再尝试；语音工作进程保留。工作进程以 `nice` 19 运行，绑定站点文件 `[unit] cpu_affinity` 的核心（模板里的 `8-15` 是还没人确认的猜测），线程数为 2（`OMP_NUM_THREADS=2`），`LEVI_GPU_LOCK_FILE` 为空。LEVI 的环境变量里它只拿到找库和找 GPU 所需的那些：没有界面令牌，也没有 Hugging Face 凭据。

## HTTP routes / HTTP 接口

All under `/api/levi/marvel0/`. All four are a person's: the web UI token is needed, an agent's Bearer credential is refused (`403 person_only`), and the site file must be valid first (`412`). Errors have the shape of the other marvel0 routes: `{"detail": {"code", "message", ...}}`; no answer names a path.

| Route                                                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET speech`                                           | `levi.marvel0.speech.status.v1`: `state` (`unavailable`, `off`, `loading`, `ready`, `busy` or `failed`), `reason` (`{code, text}` for `unavailable` and `failed`), `installed` (`worker`, `model`), `model`, `device`, `limits` (`max_seconds` 10, `max_bytes` 400000), `idle_seconds`, and `gate` (`allowed` and the `reasons` above). Passive: it never starts the worker                                                                                                                                                           |
| `POST speech/transcribe?request_id=<id>&model_id=<id>` | the body is one WAV recording (`Content-Type: audio/wav`); `request_id` is 8 to 80 characters of `A-Za-z0-9._:-`; `model_id` is optional. Answers `levi.marvel0.speech.v1`: `heard`, `reason` (`no_speech`, `language`, `low_confidence`, `empty` when `heard` is false: a normal answer, not an error), `transcript`, `language` (`English` or `Chinese`), `match` (the matcher's result: `tier`, `flag`, `family`, `best`, `alternatives`, `allowed`, `generated`, and `as_heard`: `{prompt, problem}`, see below), `engine` (`model`, `device`, `loaded_now`) and `timing_ms` (`load` and `infer` from the worker, `match` and `total` measured around the stages; all in milliseconds) |
| `POST speech/match`                                    | `{"transcript", "model_id"?}` (a closed form, at most 500 characters): the same matching for a transcript the person edited. Text only: it never starts the worker (a loaded one may translate object names). Answers `levi.marvel0.speech.match.v1` with `match` (as above) and `timing_ms` (`match`, `total`)                                                                                                                                                                                                                                                                                                                                                                     |
| `POST speech/release`                                  | stops the worker and frees its GPU memory; answers `{"state": "off", "released": true}` every time                                                                                                                                                                                                                                                                                                                                                                                                                                    |

**The recording.** Exactly a 44-byte WAV header and 16 kHz mono 16-bit samples: `RIFF`, the file size, `WAVE`, a 16-byte `fmt ` chunk, then the `data` chunk that runs to the end of the file, with both sizes matching the bytes. No other chunk, no other rate, no stereo. A bigger `Content-Length` than 400,000 is refused before anything is read, and the stream is counted as it arrives, so a sender that lies about its length or sends none is stopped at the same cap. No decoder or `ffmpeg` sees these bytes; the worker reads the samples only after this check.

| Refusal                                                                                             | Status | When                                                                                                                              |
| --------------------------------------------------------------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------- |
| `request_id_invalid`                                                                                | 400    | `request_id` missing or not 8 to 80 characters of `A-Za-z0-9._:-`                                                                 |
| `speech_body_invalid`                                                                               | 400    | `speech/match` body not the closed JSON form, or an upload cut off                                                                |
| `audio_too_short`                                                                                   | 400    | the recording is shorter than 0.3 s                                                                                               |
| `audio_too_large`                                                                                   | 413    | more than 400,000 bytes, or longer than 10.5 s                                                                                    |
| `audio_format`                                                                                      | 415    | not `audio/wav`, or not exactly the layout above                                                                                  |
| `model_unknown`                                                                                     | 422    | `model_id` is not offered by the model catalog                                                                                    |
| `speech_not_installed` / `speech_model_missing`                                                     | 503    | the worker environment / the model is not installed                                                                               |
| `speech_manifest_mismatch`                                                                          | 503    | the model files do not match their manifest                                                                                       |
| `speech_gpu_unreadable`                                                                             | 503    | see the guards above                                                                                                              |
| `speech_policy_starting`, `speech_client_connected`, `speech_arm_in_use`, `speech_gpu_insufficient` | 409    | see the guards above                                                                                                              |
| `speech_allowed_set_invalid`                                                                        | 412    | the allowed instructions cannot be told apart reliably                                                                            |
| `speech_busy`                                                                                       | 429    | one transcription at a time, no queue (`reason: in_flight`), and at most 10 a minute (`reason: rate_limit`, with `retry_after_s`) |
| `speech_failed`                                                                                     | 502    | the worker failed or answered out of turn; it was stopped and the next request starts a new one                                   |
| `speech_timeout`                                                                                    | 504    | the worker did not answer in 20 s (or load in 45 s); its whole process group was killed                                           |

全部在 `/api/levi/marvel0/` 下。四个接口都只限本人：需要网页界面令牌，智能体的 Bearer 凭据会被拒绝（`403 person_only`），并且站点文件必须先有效（否则 `412`）。错误的形状和 marvel0 的其他接口相同：`{"detail": {"code", "message", ...}}`；任何回答都不含路径。

| 接口                                                   | 作用                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET speech`                                           | `levi.marvel0.speech.status.v1`：`state`（`unavailable`、`off`、`loading`、`ready`、`busy` 或 `failed`）、`reason`（`unavailable` 和 `failed` 时是 `{code, text}`）、`installed`（`worker`、`model`）、`model`、`device`、`limits`（`max_seconds` 为 10，`max_bytes` 为 400000）、`idle_seconds`，以及 `gate`（`allowed` 和上面的 `reasons`）。被动：从不启动工作进程                                                                                                                                  |
| `POST speech/transcribe?request_id=<id>&model_id=<id>` | 请求体是一段 WAV 录音（`Content-Type: audio/wav`）；`request_id` 是 8 到 80 个 `A-Za-z0-9._:-` 字符；`model_id` 可省略。回答 `levi.marvel0.speech.v1`：`heard`、`reason`（`heard` 为 false 时是 `no_speech`、`language`、`low_confidence` 或 `empty`：这是正常结果，不是错误）、`transcript`、`language`（`English` 或 `Chinese`）、`match`（匹配器的结果：`tier`、`flag`、`family`、`best`、`alternatives`、`allowed`、`generated`，以及 `as_heard`：`{prompt, problem}`，见下）、`engine`（`model`、`device`、`loaded_now`）和 `timing_ms`（`load`、`infer` 来自工作进程，`match`、`total` 是在各阶段前后量的；单位都是毫秒） |
| `POST speech/match`                                    | `{"transcript", "model_id"?}`（封闭的表单，最多 500 个字符）：对人修改过的转写文字重新匹配。纯文本：从不启动工作进程（已加载的可以翻译物体名）。回答 `levi.marvel0.speech.match.v1`，含 `match`（同上）和 `timing_ms`（`match`、`total`）                                                                                                                                                                                                                                                                                                                                                                                       |
| `POST speech/release`                                  | 停止工作进程并释放它的显存；每次都回答 `{"state": "off", "released": true}`                                                                                                                                                                                                                                                                                                                                                                                                                            |

**录音格式。** 恰好是 44 字节的 WAV 头加 16 kHz 单声道 16 位采样：`RIFF`、文件大小、`WAVE`、16 字节的 `fmt ` 块，然后是一直到文件末尾的 `data` 块，两个大小都和字节数一致。不接受别的块、别的采样率或立体声。`Content-Length` 超过 400000 时，在读任何字节之前就拒绝；数据流到达时还会计数，所以谎报长度或不带长度的发送者也在同一个上限停下。没有任何解码器或 `ffmpeg` 接触这些字节；工作进程在这项检查之后才读采样。拒绝的错误码和状态码见上面的英文表格。

## The worker / 工作进程

`levi/stt/manager.py` keeps at most one worker process. It is started on the first transcription (never at service start), in its own session like the live segmentation worker, and recorded in `levi/children.py`, so a service that stops terminates it and a service that was killed has it reclaimed on the next start. Requests use pipes: a JSON header line and exactly `bytes` raw bytes in; one JSON line out (`ready`, `result` or `error`; the protocol is in [integrations/stt/README.md](../integrations/stt/README.md)). Its stderr goes to `<workspace>/outputs/LEVI/workbench/stt/worker.log` (the folder is private, the file is truncated at each start, and the worker keeps transcripts out of it).

- **One at a time.** A second request while one runs is refused at once, not queued.
- **Every wait has a deadline.** If the worker does not answer in time, answers out of turn, sends a line that is not JSON, too long or cut short, answers for another request, or exits, the manager kills its whole process group. The state shows `failed` with a reason (never a transcript or a worker message) until the next request starts a clean worker.
- **Idle exit.** After 120 seconds without a request the worker is asked to shut down (and killed if it does not), which frees its GPU memory. `POST speech/release` does the same on demand, and `POST /api/levi/marvel0/deploy` holds off new speech workers and waits for the current worker to exit before starting a policy server. Release errors are logged and the deploy may continue; this is not a guarantee of measured GPU reclamation. The AERI entry point does not yet share this synchronous hold.
- **Device.** A real model must report `cuda`; a worker that reports anything else is stopped. There is no CPU path: `setup.md` forbids heavy CPU jobs while the arm controller is active.

`levi/stt/manager.py` 最多保持一个工作进程。它在第一次转写时启动（从不在服务启动时），像实时分割工作进程一样放在自己的会话里，并登记在 `levi/children.py` 中，所以服务停止时会终止它，服务被杀死后下次启动会回收它。请求走管道：一行 JSON 头加恰好 `bytes` 个原始字节；回来的是一行 JSON（`ready`、`result` 或 `error`；协议见 [integrations/stt/README.md](../integrations/stt/README.md)）。它的标准错误写到 `<工作区>/outputs/LEVI/workbench/stt/worker.log`（目录是私有的，文件每次启动时截断，工作进程不往里写转写文字）。

- **同时只做一个。** 有一个在跑时再来一个，立即拒绝，不排队。
- **每次等待都有期限。** 工作进程超时没有回答、答非所问、发来的行不是 JSON、太长或被截断、回答的是另一个请求，或者退出了，管理器就杀掉它的整个进程组。状态显示 `failed` 和原因（绝不含转写文字或工作进程的话），直到下一个请求启动干净的新进程。
- **空闲退出。** 120 秒没有请求后，请工作进程关闭（不关就杀掉），释放它的显存。`POST speech/release` 按需做同样的事，`POST /api/levi/marvel0/deploy` 在启动策略服务器之前阻止新的语音工作进程，并等待当前工作进程退出；释放错误会记日志，部署仍可能继续，因此这不保证实测显存已经归还。AERI 入口尚未共享这项同步阻止机制。
- **设备。** 真实模型必须报告 `cuda`，报告别的设备的工作进程会被停掉。没有 CPU 路径：`setup.md` 禁止机械臂控制器激活时跑重 CPU 作业。

## Measured, and not verified / 实测与未验证

Measured on the RTX 5090 on 2026-10-20 with synthetic speech (Kokoro): the 1.7B model peaks at 4,878 MiB, loads in about 0.4 s warm and 1 s cold, transcribes a 2 s command in 0.08 to 0.11 s and a 10 s one in 0.24 to 0.27 s; 21 to 22 of 22 English and Chinese commands were right (two runs), and silence and noise gave nothing. The tests of this part use a stand-in worker and fake machine state: real processes and pipes, no model.

On 2026-10-27 the owner used the card on marvel0 with a UGREEN USB MIC-CM727 (chosen in the microphone list) and a real voice: the microphone and the transcription worked. That is one person, one microphone and one session, not an accuracy measurement.

**Not verified:**

- accuracy over many real voices and microphones (synthetic speech is an upper bound; mixed Chinese and English, 0 dB signal-to-noise, 1.4 times speaking speed and several people talking at once were wrong in the benchmark, which is why every transcript needs a person's confirmation);
- the effect of the worker on a running arm controller (a GPU burst of 0.1 to 0.3 s and a few threads at `nice` 19 on the site's cores, never measured with the controller active; the core list is a guess); try it with the owner present;
- the guards against a real policy server, a real client and a real arm lock: the tests build the deploy status document with fakes (the arm-lock row is the deploy's own `_check_arm`);
- that a worker exit gives the GPU memory back as measured for the benchmark processes, through this manager and a policy deploy at the 0.45 fraction;
- matching quality for instructions other than the five of the sample site file;
- translation of object names from real speech: the evaluation fed typed names, not transcripts, and did not run with an arm controller or a policy server on the machine; whether the policy acts sensibly on a translated name it never saw in training is a separate, open question;
- the microphone list in real browsers: the tests use a fake `mediaDevices` that follows the specification (hidden names before permission, `OverconstrainedError` for an `exact` id that is gone); how Chrome and Firefox name and re-number inputs on this machine, and whether the -30 dBFS "very quiet" line suits real voices, has not been checked.

用 Kokoro 合成的语音于 2026-10-20 在 RTX 5090 上实测：1.7B 模型峰值 4878 MiB，暖加载约 0.4 秒、冷加载约 1 秒，2 秒指令转写 0.08 到 0.11 秒，10 秒的 0.24 到 0.27 秒；22 句中英文指令对了 21 到 22 句（两次运行），静音和噪声没有输出。这一部分的测试用一个替身工作进程和假的机器状态：真实的进程和管道，没有模型。

2026-10-27 用户在 marvel0 上用 UGREEN USB MIC-CM727（在麦克风列表里选定）和真人声音试用了卡片：麦克风和转写都正常。这只是一个人、一只麦克风、一次试用，不是准确率测量。

**尚未验证：**

- 多种真人声音和麦克风下的准确率（合成语音只是上限；基准里中英文混说、0 dB 信噪比、1.4 倍语速和多人同时说话都会出错，所以每一条转写都需要人确认）；
- 工作进程对正在运行的机械臂控制器的影响（0.1 到 0.3 秒的 GPU 脉冲和 `nice` 19 下站点核心上的几个线程，从未在控制器激活时测过；核心列表是猜测）；请在主人在场时试；
- 对真实策略服务器、真实客户端和真实手臂锁的守卫：测试用假对象构造部署状态文档（手臂锁一行就是部署自己的 `_check_arm`）；
- 经由这个管理器、在 0.45 比例的策略部署旁边，工作进程退出后是否像基准里的进程那样归还显存；
- 站点文件示例里那五句之外的指令的匹配质量；
- 真实语音下的物体名翻译：评测输入的是打出来的名字，不是转写结果，也没有在机械臂控制器或策略服务运行时测；策略对训练里没见过的译名能否正确执行，是另一个尚未回答的问题；
- 真实浏览器里的麦克风列表：测试用的是按规范行为伪造的 `mediaDevices`（获准前不给名称，`exact` 的 ID 不存在时抛 `OverconstrainedError`）；Chrome 和 Firefox 在这台机器上怎样命名设备、何时更换设备 ID，以及 -30 dBFS 的“声音很小”界线是否适合真人声音，还没有核对。

## Fresh proposals / 提案时效

The page refreshes passive voice status every 30 seconds while visible and scopes it to the form's model. Matching and status carry an `allowed_revision` digest of the exact task list and model family. Pressing Use checks that revision again; a changed list, unavailable gate, model switch or expired proposal leaves the task field untouched. Prompt values are preserved exactly; invisible/control characters are rejected rather than rewritten. Uploading and checking disable another microphone capture. Changing the task hides the old command until the person generates a fresh preview.

页面可见时每 30 秒被动刷新语音状态，并传入表单选择的模型。匹配与状态返回任务列表原值及模型类别的 `allowed_revision` 摘要。按“使用”时再次检查该版本；列表变化、守卫不可用、模型切换或提案过期都不会填写任务。任务文本保留原值，含不可见字符或控制字符的值直接拒绝，不静默改写。上传和复核期间不能再次录音。任务变化后隐藏旧命令，直到用户重新生成预览。
