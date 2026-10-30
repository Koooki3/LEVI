# Change the next episode instruction

At the start gate of the hosted client (step 4) there is an instruction field
for the next episode. Leave it empty and press Start next episode: the episode
runs with the current instruction, shown under the field.

Type a different instruction and the Start button turns off, because it would
start the episode with the old instruction. Either clear the field ("Keep the
current instruction") or press "Change instruction and restart the client".
That opens a confirm window with the old and new instruction and the same
three checks as starting a client (a hand on the physical e-stop, the scene is
reset, the area is clear). Then LEVI:

1. presses `q` at the gate once, ending the current client. The saved episodes
   and the policy server stay;
2. waits until the same run ID reports an ended phase, a quiet state and a
   released supervisor lock;
3. starts a new client once, with the saved run form, the new instruction and
   the checks given in the window. The server checks the form again exactly as
   for any start (the task's minimum height included).

The new client stops at its start gate. Nothing moves until the person presses
Start next episode there. The client is restarted rather than switched in
place because the client switches instruction only when the next episode
starts and then writes a new session file; the page would read that as the
client running another task and hold it.

An HTTP success response only acknowledges the end request. If exit and lock
release cannot be confirmed, LEVI does not start a client and does not resend
the end action; if a later read confirms them for the same run, it starts the
new client then. If the start is refused or not answered, LEVI fills step 3
with the saved form and the new instruction, and the person starts it there
through the usual start window. An incomplete saved form blocks the change.
Finish and label an episode before changing the next instruction.

## 更改下一片段指令

托管客户端（第 4 步）的开始闸门处有一个“下一片段指令”输入栏。留空并点
“开始下一个片段”，就沿用当前指令；当前指令显示在输入栏下方。

输入了不同的指令后，“开始”按钮会变灰，因为点它会用旧指令开始片段。这时
要么清空输入栏（“沿用当前指令”），要么点“换指令并重启客户端”。确认窗口
列出新旧指令，并要求勾选和启动客户端时相同的三项（手放在实体急停上、
场景已复位、手臂周围已清空）。确认后 LEVI 会：

1. 在闸门按一次 `q`，结束当前客户端，已保存的片段和策略服务器保留；
2. 等待同一个运行 ID 显示已结束、状态静止且监管锁已释放；
3. 用已保存的运行表单、新指令和窗口里的勾选启动一次新客户端。服务器按
   任何一次启动的规则重新检查表单，包括该任务的最低高度。

新客户端停在开始闸门，用户在那里点“开始下一个片段”之前什么都不会动。之所以
重启客户端而不是原地切换：客户端要到下一个片段开始时才换指令，并会换一个
新的状态文件，页面会把这当成客户端在跑别的任务而冻结它。

HTTP 成功返回只代表结束请求已收到。如果无法确认客户端退出和锁释放，LEVI
不会启动客户端，也不会重发结束操作；之后某次读取确认了同一运行的退出，
再启动新客户端。如果启动被拒绝或没有回应，LEVI 会把已保存的表单和新指令
回填到第 3 步，由用户通过常规启动窗口启动。已保存的表单不完整时不能更改。
更改下一条指令前，请先完成并标注当前片段。
