# Speech input for the marvel0 page / marvel0 页面的语音输入

The demo client command card of the **marvel0** page can take the task instruction by voice. You hold a button and say the instruction; a speech model on this machine turns it into text; LEVI finds the closest of the **allowed** task instructions; and when you click **Use**, that sentence from the list (never the transcript) is put into the task instruction field. From there it is what typing would have done: you click **Make command**, copy it, and press Enter in the client terminal yourself. Nothing here starts the client or the arm.

marvel0 页面的评测客户端命令卡片可以用语音填写任务指令。按住按钮说出指令，本机上的语音模型把它转成文字，LEVI 在**允许的**任务指令里找最接近的一句；你点击“使用”之后，填进任务指令框的是列表里的那句原文（绝不是转写文字）。之后和手打一样：你点“生成命令”、复制，再自己在客户端终端按 Enter。这里没有任何一步会启动客户端或机械臂。

## What it does, and never does / 做什么，绝不做什么

It does:

- record while a button is held (at most 10 seconds) as 16 kHz mono 16-bit WAV, in the browser, from the microphone chosen on the card (the browser's default unless another one is chosen, see below); the page must be opened as `http://127.0.0.1:7860` or `http://localhost:7860` (a browser gives the microphone to no other address);
- transcribe it with `Qwen3-ASR-1.7B` (Apache-2.0) in a **separate worker process** on this machine's GPU, offline, started on the first recording and gone after two minutes without one;
- compare the transcript with the allowed instructions (below) and offer the closest: tier **A** pre-selects one, **B** offers the best three, **C** lists all of them, **V** (a stop, negation or unsafe word) offers none;
- change the task instruction field only when you click **Use**.

It never:

- starts, stops or connects to the policy server, the client or the arm stack, presses Enter, or sends a motion command; the sequence **Use**, **Make command**, **Copy** is three separate clicks of yours;
- stops or pauses the robot. There is no voice stop and nothing on the page listens for one. **Speech cannot stop the arm: use the physical e-stop.** The page says so next to the card, permanently;
- changes any other field (`--min-z`, RTC, steps, episode count, rollout group, note, the confirmation phrases of the deploy card, the automatic-evaluation wizard);
- puts the transcript into a command. The form check accepts almost any text for a non-restricted model, so the speech path is stricter by design: what can reach the field is a sentence of the allowed list, or the sentence LEVI generated in the datasets' most common phrasing, shown as generated (see [Generated sentences](#generated-sentences--生成句)); never the transcript itself;
- listens continuously, waits for a wake word, or keeps the microphone open between recordings;
- keeps audio or text. The audio exists in memory and in the worker's pipe; the transcript only in the answer to the page (and in the page's memory for 60 seconds). Neither goes to a file, a log line, an error message or a URL. The log gets the request id, an outcome word, byte counts and eight characters of a SHA-256;
- sends anything off the machine: the model runs offline (`HF_HUB_OFFLINE=1`), and no cloud recogniser and no language model is involved;
- takes the physical GPU lock or uses `levi/inference/gpu.py` (either would stop a policy deploy or refuse exactly while the policy server runs).

它会做的：

- 按住按钮时在浏览器里录音（最长 10 秒），格式是 16 kHz 单声道 16 位 WAV，用的是卡片上选定的麦克风（没有另选时是浏览器的默认麦克风，见下）；页面必须以 `http://127.0.0.1:7860` 或 `http://localhost:7860` 打开（浏览器不会把麦克风给任何其他地址）；
- 用 `Qwen3-ASR-1.7B`（Apache-2.0）在本机 GPU 上的**独立工作进程**里转写，离线运行；第一次录音时启动，两分钟没有新录音就退出；
- 把转写文字和允许的指令（见下）比较并给出最接近的：A 档预选一句，B 档给出最好的三句，C 档列出全部，V 档（出现“停”、否定或不安全的词）不给候选；
- 只有你点击“使用”时才改任务指令框。

它绝不做的：

- 启动、停止或连接策略服务器、客户端或机械臂栈，按 Enter，或发运动命令；“使用”“生成命令”“复制”是你的三次独立点击；
- 让机器人停下或暂停。没有语音停机，页面上也没有任何东西在听停止词。**语音不能让机械臂停下，请按物理急停。** 页面在卡片旁永久写着这句话；
- 改任何其他字段（`--min-z`、RTC、步数、片段数、rollout 组、备注、部署卡片的确认短语、自动评测向导）；
- 把转写文字放进命令。非受限模型的表单检查几乎放过任何文字，所以语音这条路故意更严：能进入输入框的只有允许列表里的句子，或者 LEVI 按数据集最常见句式生成、并标明是生成句的那一句（见[生成句](#generated-sentences--生成句)）；转写原文本身永远不会进去；
- 持续监听、等唤醒词，或在两次录音之间保持麦克风打开；
- 保存音频或文字。音频只存在于内存和工作进程的管道里；转写文字只存在于给页面的回答里（以及页面内存里 60 秒）。两者都不写入文件、日志、错误信息或 URL。日志只有请求 ID、一个结果词、字节数和 SHA-256 的前八个字符；
- 把任何东西发到本机之外：模型离线运行（`HF_HUB_OFFLINE=1`），不涉及云端识别，也不涉及语言模型；
- 取物理 GPU 锁或使用 `levi/inference/gpu.py`（前者会挡住策略服务器部署，后者恰好在策略服务器运行时拒绝）。

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
- an object outside the lexicon said in English is kept as said ("dragon fruit", one to three words), but only after a place word ("into", "on") when it is the destination, and never a direction or manner word ("left", "back", "quickly"); one said in Chinese needs an English name first (a small offline translation model is the planned second stage), and until then the card says which names had none;
- when one step names a specific action and a move ("pick up the cup and pour it into the bowl") the specific one is the task; two specific actions in one step give no sentence;
- a word no slot can hold (a number, a word in another script) gives no sentence (`unclear`), so nothing heard is dropped silently;
- the frames are learned from every instruction, reset instructions (`Reset: ...`) included, since they are the same robot's phrasing; the episode count shown includes them.

What keeps it safe: the block is labelled "generated: not an instruction the policy was trained on" and shows the frame and how many episodes use it; only its own **Use** press hands it over, after the same 800 ms and freshness checks as any proposal; a dataset instruction with the same meaning is shown beside it exactly as the dataset wrote it, since the policy may know that wording better, and only a task instruction (never a `Reset:` one) that the matcher reads without a flag or an unknown word; a word the matcher guessed from a likely recognition mistake ("pearl" read as "pear") is listed as a guess (`repairs`, slot source `repair`); nothing is generated for a result with a stop, negation or unsafe flag, or for a model of the restricted family (both the server and the page enforce this); a frame that cannot hold the request ("same colour", "corresponding", "from ...", one object without a place) gives no sentence and says why; and the sentence is read back with the matcher's reader and must contain exactly the heard objects with their colours and sizes. Speech still never starts, stops or moves anything. The answer field is `match.generated`: `status` (`ok`, `vetoed`, `restricted`, `no_task`, `unsupported`, `no_frame`, `untranslated`, `unclear`), `prompt`, `frames`, `episodes`, `slots` (`said`, `english`, `source`: `lexicon`, `repair`, `english`, `translation`), `untranslated`, `dataset_original`, `repairs` (`heard`, `as`). A malformed row of a dataset is skipped and an unreadable dataset is left out; neither stops generation.

维护者 2026-10-30 决定：除了最接近的允许指令，卡片还给出一句按所说内容**生成**的句子，用已登记数据集里最常见的说法，而不是只检索一句已有的。`levi/marvel0/speech_template.py`（只用标准库）通过匹配器的词表读出转写里的动作和物体（中英文都可以），填进从目录里所有数据集的任务指令中学到的句式；统计按片段数加权（忽略 `Reset:` 前缀，命名空间不重复计数；数据集版本变化时重新读取）：

- 动词取这个动作最常用的，例如放置用 `pick`，还有 `stack`、`pour`；
- 介词跟着目标物体走，和数据集的写法一致：碗和抽屉用 `into`，篮子用 `in`，盘子用 `on`；动作有自己固定用法的保留自己的（杯子叠放用 `stack ... on`，即使倒水是 `into`）；
- 用 then / 然后 连接的几步，生成用 `then` 连接的几个句式；没说动词的一步沿用前一步的动作；
- 词表外的物体用英文说出时照原样保留（“dragon fruit”，一到三个词），但它作为目标时前面必须有介词（“into”“on”），而且不能是方向或方式词（“left”“back”“quickly”）；用中文说出的需要先有英文名（计划第二期用一个小型离线翻译模型），在那之前卡片会说明哪些名字没有英文；
- 一步里同时有具体动作和“放”（“拿起杯子倒进碗里”）时，以具体动作为准；一步里有两个具体动作则不生成；
- 有放不进任何槽位的词（数字、其他文字的词）时不生成（`unclear`），听到的内容不会被悄悄丢掉；
- 句式从所有指令里学，包括复位指令（`Reset: ...`），因为它们是同一台机器人的说法；显示的片段数也包含它们。

安全措施：这一块标明“模板生成，不在训练过的指令里”，并显示句式和用它的片段数；只有点它自己的**使用**才会交出，和其他提案一样要过 800 毫秒和时效检查；数据集里意思相同的原句按数据集原样并排给出，因为策略可能更熟悉那个说法，而且只给任务指令（不给 `Reset:` 指令）、匹配器读来没有标记也没有未知词的句子；匹配器根据常见识别错误猜出来的词（“pearl”读成“pear”）会作为猜测列出（`repairs`，槽位来源 `repair`）；结果带停止、否定或不安全标记时，以及受限族模型，都不生成（服务端和页面都检查）；句式装不下的请求（“相同颜色”“对应”“从……”、只有一个物体没有目标）不给句子并说明原因；生成的句子会用匹配器的读取器回读，必须正好包含听到的物体及其颜色和大小。语音仍然不能启动、停止或移动任何东西。回答字段是 `match.generated`：`status`（`ok`、`vetoed`、`restricted`、`no_task`、`unsupported`、`no_frame`、`untranslated`、`unclear`）、`prompt`、`frames`、`episodes`、`slots`（`said`、`english`、`source`：`lexicon`、`repair`、`english`、`translation`）、`untranslated`、`dataset_original`、`repairs`（`heard`、`as`）。数据集里格式不对的行会跳过，读不了的数据集不计入，都不会让生成停下。

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

`setup.sh` puts the model into `$LEVI_WORKSPACE/checkpoints/stt/qwen3-asr-1.7b-hf/` (never into the shared Hugging Face cache) and writes its `MANIFEST.sha256`; the worker checks every file against it each time it loads the model and refuses to start on a mismatch (`speech_manifest_mismatch`). More in [integrations/stt/README.md](../integrations/stt/README.md).

工作进程有自己的环境；LEVI 的核心从不导入 Torch。`setup.sh` 把模型放进 `$LEVI_WORKSPACE/checkpoints/stt/qwen3-asr-1.7b-hf/`（不写共享的 Hugging Face 缓存）并写出 `MANIFEST.sha256`；工作进程每次加载模型时都按它核对每个文件，不符就拒绝启动（`speech_manifest_mismatch`）。详见 [integrations/stt/README.md](../integrations/stt/README.md)。

## Settings / 设置

| Setting                  | Default                             |                                                                                                                                                                                                                                                     |
| ------------------------ | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LEVI_STT_WORKER_PYTHON` | `integrations/stt/.venv/bin/python` | the worker environment's Python / 工作进程环境的 Python                                                                                                                                                                                             |
| `LEVI_STT_MODEL_DIR`     | `$LEVI_WORKSPACE/checkpoints/stt`   | the folder of model folders (must be inside the workspace) / 存放模型文件夹的目录（必须在工作区内）                                                                                                                                                 |
| `LEVI_STT_MODEL`         | `qwen3-asr-1.7b-hf`                 | the model folder's name; `fake` runs the worker's `fake` provider (LEVI's own Python, no model, no GPU) for tests and demonstrations / 模型文件夹名；`fake` 运行工作进程的 `fake` 提供者（用 LEVI 自己的 Python，没有模型、不用 GPU），供测试和演示 |
| `LEVI_STT_FAKE_SCRIPT`   | unset                               | with `fake`: a JSON file of scripted answers, consumed in order (tests only) / 配合 `fake`：按顺序消费的脚本答案 JSON 文件（仅测试用）                                                                                                              |

Everything else is a constant: recordings of 0.3 to 10.5 s and at most 400,000 bytes; one transcription at a time and at most 10 a minute; idle exit after 120 s; 45 s to start and 20 s per transcription (the browser allows 75 s in total); free GPU memory of 5000 MiB plus the site margin for a cold start. The worker runs at `nice` 19, on the cores of the site file's `[unit] cpu_affinity` (the template's `8-15` is a guess nobody has confirmed), with 2 threads (`OMP_NUM_THREADS=2`) and `LEVI_GPU_LOCK_FILE` empty. Of LEVI's environment it receives only what it needs to find its libraries and the GPU: not the UI token, not Hugging Face credentials.

其余都是常量：录音 0.3 到 10.5 秒、不超过 400000 字节；同时只处理一次转写，每分钟最多 10 次；空闲 120 秒后退出；启动限时 45 秒，每次转写限时 20 秒（浏览器总期限为 75 秒）；冷启动需要 5000 MiB 加站点文件余量的空闲显存。工作进程以 `nice` 19 运行，绑定站点文件 `[unit] cpu_affinity` 的核心（模板里的 `8-15` 是还没人确认的猜测），线程数为 2（`OMP_NUM_THREADS=2`），`LEVI_GPU_LOCK_FILE` 为空。LEVI 的环境变量里它只拿到找库和找 GPU 所需的那些：没有界面令牌，也没有 Hugging Face 凭据。

## HTTP routes / HTTP 接口

All under `/api/levi/marvel0/`. All four are a person's: the web UI token is needed, an agent's Bearer credential is refused (`403 person_only`), and the site file must be valid first (`412`). Errors have the shape of the other marvel0 routes: `{"detail": {"code", "message", ...}}`; no answer names a path.

| Route                                                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET speech`                                           | `levi.marvel0.speech.status.v1`: `state` (`unavailable`, `off`, `loading`, `ready`, `busy` or `failed`), `reason` (`{code, text}` for `unavailable` and `failed`), `installed` (`worker`, `model`), `model`, `device`, `limits` (`max_seconds` 10, `max_bytes` 400000), `idle_seconds`, and `gate` (`allowed` and the `reasons` above). Passive: it never starts the worker                                                                                                                                                           |
| `POST speech/transcribe?request_id=<id>&model_id=<id>` | the body is one WAV recording (`Content-Type: audio/wav`); `request_id` is 8 to 80 characters of `A-Za-z0-9._:-`; `model_id` is optional. Answers `levi.marvel0.speech.v1`: `heard`, `reason` (`no_speech`, `language`, `low_confidence`, `empty` when `heard` is false: a normal answer, not an error), `transcript`, `language` (`English` or `Chinese`), `match` (the matcher's result: `tier`, `flag`, `family`, `best`, `alternatives`, `allowed`), `engine` (`model`, `device`, `loaded_now`) and `timing_ms` (`load`, `infer`) |
| `POST speech/match`                                    | `{"transcript", "model_id"?}` (a closed form, at most 500 characters): the same matching for a transcript the person edited. Text only; the worker is not touched                                                                                                                                                                                                                                                                                                                                                                     |
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
| `POST speech/transcribe?request_id=<id>&model_id=<id>` | 请求体是一段 WAV 录音（`Content-Type: audio/wav`）；`request_id` 是 8 到 80 个 `A-Za-z0-9._:-` 字符；`model_id` 可省略。回答 `levi.marvel0.speech.v1`：`heard`、`reason`（`heard` 为 false 时是 `no_speech`、`language`、`low_confidence` 或 `empty`：这是正常结果，不是错误）、`transcript`、`language`（`English` 或 `Chinese`）、`match`（匹配器的结果：`tier`、`flag`、`family`、`best`、`alternatives`、`allowed`）、`engine`（`model`、`device`、`loaded_now`）和 `timing_ms`（`load`、`infer`） |
| `POST speech/match`                                    | `{"transcript", "model_id"?}`（封闭的表单，最多 500 个字符）：对人修改过的转写文字重新匹配。纯文本，不碰工作进程                                                                                                                                                                                                                                                                                                                                                                                       |
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
- the microphone list in real browsers: the tests use a fake `mediaDevices` that follows the specification (hidden names before permission, `OverconstrainedError` for an `exact` id that is gone); how Chrome and Firefox name and re-number inputs on this machine, and whether the -30 dBFS "very quiet" line suits real voices, has not been checked.

用 Kokoro 合成的语音于 2026-10-20 在 RTX 5090 上实测：1.7B 模型峰值 4878 MiB，暖加载约 0.4 秒、冷加载约 1 秒，2 秒指令转写 0.08 到 0.11 秒，10 秒的 0.24 到 0.27 秒；22 句中英文指令对了 21 到 22 句（两次运行），静音和噪声没有输出。这一部分的测试用一个替身工作进程和假的机器状态：真实的进程和管道，没有模型。

2026-10-27 用户在 marvel0 上用 UGREEN USB MIC-CM727（在麦克风列表里选定）和真人声音试用了卡片：麦克风和转写都正常。这只是一个人、一只麦克风、一次试用，不是准确率测量。

**尚未验证：**

- 多种真人声音和麦克风下的准确率（合成语音只是上限；基准里中英文混说、0 dB 信噪比、1.4 倍语速和多人同时说话都会出错，所以每一条转写都需要人确认）；
- 工作进程对正在运行的机械臂控制器的影响（0.1 到 0.3 秒的 GPU 脉冲和 `nice` 19 下站点核心上的几个线程，从未在控制器激活时测过；核心列表是猜测）；请在主人在场时试；
- 对真实策略服务器、真实客户端和真实手臂锁的守卫：测试用假对象构造部署状态文档（手臂锁一行就是部署自己的 `_check_arm`）；
- 经由这个管理器、在 0.45 比例的策略部署旁边，工作进程退出后是否像基准里的进程那样归还显存；
- 站点文件示例里那五句之外的指令的匹配质量；
- 真实浏览器里的麦克风列表：测试用的是按规范行为伪造的 `mediaDevices`（获准前不给名称，`exact` 的 ID 不存在时抛 `OverconstrainedError`）；Chrome 和 Firefox 在这台机器上怎样命名设备、何时更换设备 ID，以及 -30 dBFS 的“声音很小”界线是否适合真人声音，还没有核对。

## Fresh proposals / 提案时效

The page refreshes passive voice status every 30 seconds while visible and scopes it to the form's model. Matching and status carry an `allowed_revision` digest of the exact task list and model family. Pressing Use checks that revision again; a changed list, unavailable gate, model switch or expired proposal leaves the task field untouched. Prompt values are preserved exactly; invisible/control characters are rejected rather than rewritten. Uploading and checking disable another microphone capture. Changing the task hides the old command until the person generates a fresh preview.

页面可见时每 30 秒被动刷新语音状态，并传入表单选择的模型。匹配与状态返回任务列表原值及模型类别的 `allowed_revision` 摘要。按“使用”时再次检查该版本；列表变化、守卫不可用、模型切换或提案过期都不会填写任务。任务文本保留原值，含不可见字符或控制字符的值直接拒绝，不静默改写。上传和复核期间不能再次录音。任务变化后隐藏旧命令，直到用户重新生成预览。
