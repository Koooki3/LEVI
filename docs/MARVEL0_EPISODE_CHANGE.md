# Change the next episode instruction

At the start gate after an episode has been saved, step 4 can end the current
hosted client and prepare a new command with a different instruction. The
saved episodes and policy server are retained. LEVI waits for the same run ID
to report an ended phase, a quiet state, and a released supervisor lock before
it fills step 3. The saved run form supplies every other parameter; an
incomplete form blocks the action.

An HTTP success response only acknowledges the end request. If exit and lock
release remain unknown, LEVI does not fill the new form and does not resend the
end action. It never presses Enter or starts a client. The person must make the
command and complete the usual separate start confirmation. Finish and label an
episode before changing its next instruction.

## 更改下一片段指令

片段保存后，在第 4 步的开始闸门处可以结束当前托管客户端，并用新指令
准备下一条命令。已保存的片段和策略服务器会保留。LEVI 会等待同一个运行
ID 显示已结束、状态静止且监管锁已释放，才回填第 3 步。其他参数全部来自
已保存的运行表单；表单不完整时会阻止此操作。

HTTP 成功返回只代表结束请求已收到。如果无法确认客户端退出和锁释放，
LEVI 不会回填新表单，也不会重发结束操作。它不会按回车或启动客户端。
用户仍需自行生成命令，并单独完成常规启动确认。更改下一条指令前，请先
完成并标注当前片段。
