# 设计系统

[English](DESIGN.md)

LEVI 界面分阶段重构：石墨强调色，浅色和深色两套外观、默认跟随系统，只用系统字体，图标用 Lucide，Motion 只用于拖拽和列表重排。第 1 阶段加入了设计令牌、主题偏好和一组基础组件。第 2 阶段（见[全局框架](#全局框架第-2-阶段)）把全局框架迁到上面：顶栏、Toast、确认对话框、命令面板、快捷键总表和 Agent 工作台抽屉。第 5 阶段的全局部分（见[品牌标识](#品牌标识)和[全局页面基础](#全局页面基础第-5-阶段)）统一了标识，把页面基础和所有旧页面样式接到令牌上，并重做了首页、使用指南和报告页。片段查看器和其他页面在各自阶段把标记换成 `ds-*` 组件；在那之前，它们通过映射到令牌的旧名称跟随两套外观。

| 内容 | 位置 |
| --- | --- |
| 令牌（CSS 变量 `--ds-*`） | `src/styles/tokens.css` |
| 组件样式（类名 `ds-*`） | `src/styles/ds.css` |
| 组件 | `src/components/ds/`（`index.ts` 导出除 `ReorderList` 外的全部组件） |
| 主题偏好 | `src/lib/design/theme.ts` |
| 减少动态效果的工具、时长、断点 | `src/lib/design/motion.ts` |
| 样张页开关 | `src/lib/design/gate.ts`、`src/middleware.ts` |
| 样张页（仅开发用） | `/design`（`src/app/design/`） |
| 全局框架（第 2 阶段） | `src/components/shell/`、`src/components/levi-header.tsx`、`src/styles/shell.css` |
| 标识（唯一定义） | `src/components/shell/brand.tsx`；浏览器标签图标由 `scripts/brand_icons.py` 据此生成（`src/app/icon.svg`、`apple-icon.png`、`favicon.ico`） |
| 页面基础与旧名称（第 5 阶段） | `src/app/globals.css`、`src/app/levi.css` |
| 首页、使用指南、报告页（第 5 阶段） | `src/components/home/`、`src/styles/home.css`；`src/app/guide/`、`src/styles/reading.css`；`src/components/report/`、`src/app/report/report.css` |
| 代码里取令牌值（SVG、canvas） | `src/lib/design/css-tokens.ts`（`useCssTokens`） |
| 测试 | `src/components/ds/__tests__/`、`src/lib/design/__tests__/`、`src/components/shell/__tests__/` |

## 新代码的规则

- **禁止硬编码颜色。** 用语义令牌（`var(--ds-text-secondary)`、`var(--ds-surface-1)`）或 `ds-*` 类。新的 CSS 和 TSX 里不写十六进制、`rgb()`、`hsl()`，也不写 Tailwind 任意值；`ds.css`、`shell.css`、样张页 CSS、组件和样张页的 TSX 里出现就会让测试失败；全局框架的 TSX 里出现十六进制颜色时 ESLint 报错（`no-restricted-syntax`，文件清单是 `eslint.config.mjs` 的 `FRAME_FILES`；颜色指字符串开头或空格、`(`、`,`、`:` 之后的 `#` 加 3、4、6 或 8 位十六进制数字；`href`、`to`、`id`、`htmlFor` 的值是链接，不检查；测试在 `src/__tests__/eslint-hex.test.ts`）。旧页面在第 6 阶段清理。数据配色（时间片段、掩码、图表序列）是单独一套，第 4 阶段定义。
- **组件里只用语义令牌。** 原始灰阶 `--ds-gray-l-*`、`--ds-gray-d-*` 只用来定义语义令牌。
- **每屏一个主要按钮。** 强调色 A“石墨”是最深的灰，只用于主要按钮、焦点环、选中态和进度。正文中的链接用主文字色加下划线。
- **状态不只靠颜色。** 状态色（成功、警告、错误、信息）只出现在徽章、状态点、Toast 和行内提示上，并且总带图标形状和文字（`Badge`、`StatusDot`）。
- **最小字号 12 px**（`--ds-text-caption-size`）；字重只用 400、500、600。
- **图标**只用 Lucide，经 `Icon` 引入（16 px 配描边 1.75；20、24 px 配 1.5）。装饰性图标 `aria-hidden`；承载含义的图标给 `label`。
- **不用 `title=` 做提示。** 用 `Tooltip`（悬停和键盘焦点都会出现）；只有图标的按钮用 `IconButton`，它必须有 `label`（即 `aria-label` 和提示文字）。
- **不用 `window.confirm`。** 通过 `useConfirmAction()`（`src/components/shell/confirm.tsx`）询问，`src/` 里出现 `window.confirm` 会让测试失败；只有不可逆的操作才询问；预期内、可撤销的删除直接删除，并在 Toast 里给“撤销”。
- **文案**走语言目录（`useLocale().t`，`en.json` 和 `zh.json` 同时加）。组件的默认文字（Close、Cancel、Loading、In progress、Dismiss notification、Notifications、Move、Theme、System、Light、Dark）已经在目录里。

## 令牌

全部令牌是 `:root` 上的 CSS 自定义属性，统一以 `--ds-` 开头，不会与旧的 `--bg`、`--accent`、`--surface-*` 冲突。

| 分组 | 令牌 |
| --- | --- |
| 中性灰阶 | 浅色 `--ds-gray-l-0…11`，深色 `--ds-gray-d-0…12`（略带冷调的中性灰，提案 §4.1） |
| 表面 | `--ds-bg`、`--ds-bg-reading`、`--ds-surface-sunken`、`--ds-surface-1`（卡片）、`--ds-surface-2`（抬升：菜单、对话框）、`--ds-surface-hover`、`--ds-surface-hover-on-raised`、`--ds-surface-selected`、`--ds-field-bg`、`--ds-skeleton`、`--ds-media-bg`（两套都是黑）、`--ds-scrim` |
| 线 | `--ds-separator`、`--ds-separator-strong`、`--ds-border-control`、`--ds-border-control-on-raised` |
| 文字 | `--ds-text-primary`、`--ds-text-secondary`、`--ds-text-tertiary`、`--ds-text-tertiary-on-sunken`、`-on-hover`、`-on-selected`、`-on-raised`、`--ds-text-disabled`、`--ds-icon` |
| 强调色 A | `--ds-accent`、`--ds-accent-hover`、`--ds-on-accent`、`--ds-focus-ring`、`--ds-selected-indicator`、`--ds-progress`、`--ds-progress-track` |
| 状态 | `--ds-success`、`--ds-warning`、`--ds-danger`、`--ds-info`，各带 `-bg`；`--ds-on-danger` |
| 数据 | `--ds-data-1…6`：图表的序列和类别颜色，两套外观下对页面底和卡片都至少 3:1（有测试）；不用于界面状态 |
| 媒体 | `--ds-media-bg`（黑）、`--ds-on-media`、`--ds-on-media-secondary`（视频和图像上的白色文字，两套外观相同；Tailwind `text-on-media`）、`--ds-media-scrim` |
| 层次与材质 | `--ds-shadow-1…3`、`--ds-ring-raised`（深色）、`--ds-material-bar`、`--ds-material-filter` |
| 字体 | `--ds-font-sans`、`--ds-font-mono`；`--ds-text-{display,title-1,title-2,title-3,body,reading,callout,caption}-{size,line}`；`--ds-weight-{regular,medium,semibold}`；`--ds-tracking-{title,display}` |
| 形状与间距 | `--ds-radius-{xs,sm,md,lg,full}`（4、6、10、14、999 px）；`--ds-space-{0-5,1,2,3,4,5,6,8,10,12,16}`（4 px 基数）；`--ds-control-{sm,md,lg}`（28、32、40 px）；`--ds-hit-min`；`--ds-icon-{sm,md,lg}` |
| 动效 | `--ds-dur-{instant,fast,base,base-exit,slow,slow-exit}`（0、120、200、140、320、220 ms）；`--ds-ease-{standard,exit,spring}`；`--ds-motion-{shift,toast-shift,dialog-scale,press-scale,drawer-shift}` |
| 层级 | `--ds-z-{base,sticky,dock,popover,overlay,dialog,toast}`（0–60） |

断点（640、900、1200、1440 px）是 `src/lib/design/motion.ts` 里的常量 `BREAKPOINT`，因为媒体查询读不到 CSS 变量。

**对比度。** 两套主题下，文字配对都达到 4.5:1，控件边界、焦点环、选中竖条和进度达到 3:1；`tokens-contrast.test.ts` 直接从 `tokens.css` 读取数值，用 WCAG 公式计算。提案判为不达标的配对用专门的令牌替代：凹陷区、悬停行、选中行和抬升层里的三级文字，深色抬升层里的控件边界，以及深色抬升层里的悬停底（gray-6，比抬升表面 gray-5 更亮）。有两个状态色与提案不同，以便在悬停底上也达标：浅色“信息”`#3a6693`（原 `#3d6a99`，在悬停底上 4.41:1），深色“错误”`#ee8f80`（原 `#e8806f`，在抬升层悬停底上 4.19:1）。状态色对卡片、抬升层、输入框底、悬停底、抬升层悬停底都做检查，危险按钮悬停色对其文字也做检查。在 `Card variant="sunken"`、菜单、对话框、抽屉、提示、Toast，以及悬停或选中的表格行里，这些替换自动生效（`ds-on-sunken`、`ds-on-raised`）；其他画在这些表面上的容器，自己加上 `ds-on-sunken` 或 `ds-on-raised` 类。

**层次。** 相邻表面差一到两级灰；卡片始终有 1 px 分隔线描边（浅色卡片与背景只有 1.09:1）。阴影用于浅色；深色用更亮的表面加内描边。玻璃材质（`ds-material`）只给浮在内容上的功能层（顶栏、浮动工具条）；在 `prefers-reduced-transparency` 或 `prefers-contrast: more` 下变为不透明。

## 主题

默认浅色。系统为深色（`prefers-color-scheme: dark`）且祖先元素没有 `data-theme="light"` 时用深色；`data-theme="dark"` 下总是深色。`data-theme` 可以放在任何元素上，单独给一个子树换主题（样张页就是这样并排显示两套）。`prefers-contrast: more` 时，分隔线、控件边界和次要文字各提高一级。

`useThemePreference()` 返回 `{ preference, resolved, setPreference }`：取值 `"system" | "light" | "dark"`，存在 `localStorage` 的 `levi-theme` 键下。任何显式选择都会保存，包括与默认值相同的选择（默认值以后改了，这个选择仍然有效）；只有从没选过的人没有存储值，这时（或存储不可用时）取 `THEME_DEFAULT_PREFERENCE`。**过渡期默认深色。** 页面改用令牌（第 5 阶段）之前，浅色顶栏压在深色页面上很别扭，所以默认是 `"dark"`；第 5 阶段改回 `"system"`（跟随系统）。这个值是 `src/lib/design/theme.ts` 里的一个常量，`theme-boot.ts` 里重复了一份，有测试保证两者相同。读写都经过不会抛异常的 `browserStorage`。它跟随系统外观的变化和其他标签页的修改。这个 hook 本身不改 `<html>`，`applyTheme(element, preference)` 才改。从第 2 阶段起，全局框架（`ShellProvider`）持有唯一的偏好并把它作用到 `<html>`；根布局 `<head>` 里的一小段脚本（`theme-boot.ts`）在首次绘制前应用同一个偏好（已存浅色/深色就写入；已存“跟随系统”就不写；没有存储值时用默认值），顶栏不会先闪一下另一套外观。原生控件跟随主题：`<html>` 的 `color-scheme` 为 `light dark`，有 `data-theme` 时按它设置（第 5 阶段起；之前固定为深色）。`ThemePicker` 是“跟随系统 / 浅色 / 深色”的切换控件；顶栏用一个有同样三个选项的菜单。

给容器加 `ds-root` 类，它就使用设计系统的字体、文字色和背景，`color-scheme` 也随主题变化。

## 组件

从 `@/components/ds` 引入。根布局为所有页面引入一次 `@/styles/tokens.css`、`@/styles/ds.css` 和 `@/styles/shell.css`。

| 组件 | 说明 |
| --- | --- |
| `Button` | `variant` 主要 / 次要（默认）/ 幽灵 / 危险；`size` sm / md / lg（28 / 32 / 40 px）；`loading`（旋转图标、`aria-busy`、忽略点击、保留文字）；`icon`、`iconEnd`；默认 `type="button"` |
| `IconButton` | `label` 必填（无障碍名称和提示文字）；`shortcut`；切换按钮用 `pressed`（`aria-pressed`） |
| `Icon` | Lucide 图标，`size` sm / md / lg；给 `label` 时为 `role="img"` |
| `Tooltip` | 悬停 0.5 秒后出现，键盘获得焦点时立即出现（点击不出现）；Esc、指针移开、失去焦点时关闭；给触发元素设 `aria-describedby` |
| `Field`、`Input`、`Textarea`、`Select` | `Field` 把标签、提示和错误关联到内部控件（`aria-describedby`、`aria-invalid`、`required`）；`Select` 是原生下拉框 |
| `Checkbox`、`Radio`、`RadioGroup`、`Switch` | 原生输入控件；`Checkbox indeterminate` 表示部分选中；`RadioGroup` 是带 legend 的 fieldset；`Switch` 是 `role="switch"` 的复选框 |
| `Badge`、`StatusDot`、`Tag` | 状态用图标形状 + 颜色 + 文字表示（`tone`：neutral、success、warning、danger、info）；`StatusDot live` 每 2 秒呼吸一次（减少动态效果时静止）；`Tag onRemove` 带“Remove …”按钮，可点区域 24 × 24 px（视觉 16 px）；标签内容不是纯文本时必须给 `removeLabel`（类型检查强制） |
| `Card`、`Divider`、`Kbd` | `Card` 的 `variant` 为 default / sunken / raised，`padding` 为 compact / regular，可选 `title`、`description`、`actions` |
| `Dialog`、`Sheet` | `Sheet modal={false}` 是页面旁边的抽屉：没有遮罩、不困住焦点，只有焦点在抽屉内时 Esc 才关闭它，焦点仍会移入并在关闭后返回；`width` 设宽度（px）。其余情况为模态（`aria-modal`），以标题命名；焦点移入，Tab 在内部循环，焦点落到外面（点了遮罩、别处调用了 `focus()`）会被拉回，Esc 和点遮罩关闭（`closeOnScrim={false}` 时点遮罩不关），关闭后焦点回到打开它的元素；嵌套时只有最上层处理 Tab 和 Esc，内部菜单先处理自己的 Esc；`container` 可渲染到别的元素（portal）；`Sheet side` 为 right / left / bottom |
| `ConfirmDialog`、`useConfirm` | `role="alertdialog"`；标题写清动作和对象，`confirmLabel` 用动词；`tone="danger"` 时默认焦点在“取消”；`const { confirm, dialog } = useConfirm()` 后 `await confirm({...})`；一个问题还开着时又问第二个，第一个按“取消”作答（返回 false）。应用代码改用全局框架的 `useConfirmAction()` |
| `ToastProvider`、`useToast` | 右下角；`show({ title, description, tone, action, duration })`；普通通知走 polite 区域，最多 3 条，4 秒后消失（悬停或聚焦时暂停）；带操作按钮的 Toast 默认不自动消失（除非给 `duration`）；错误（`danger`）走 assertive 区域，不会被新通知挤掉，直到手动关闭；在 provider 之外 `show` 什么也不做 |
| `Skeleton`、`SkeletonText` | 静态灰块（不扫光），对辅助技术隐藏；正在加载的区域标 `aria-busy="true"` |
| `Progress`、`Spinner` | `Progress value={n}` 为确定进度；`value={null}` 为不确定进度（一段来回移动的条，减少动态效果时静止并显示“进行中”）；`Spinner` 是 `role="status"`；两者都不是对话框。局限：每个 `Spinner` 自己是一个 live 区域，而与文字一起插入的 live 区域并非所有读屏软件都会播报；必须让人听到的结果，请保持一个常驻的状态区域只改其文字，或用 Toast |
| `EmptyState` | 图标、一句说明、下一步操作按钮（`action`、`secondaryAction`） |
| `Tabs` | ARIA 标签页：一个 Tab 停靠点，←/→、Home/End，跳过禁用项；切换立即生效；只有选中的标签写 `aria-controls`；`value` 找不到可用项时选中第一个未禁用的标签 |
| `SegmentedControl` | 画成拼接按钮的单选组；方向键移动并选中 |
| `Menu` | 菜单按钮：Enter/Space/↓ 打开并聚焦第一项，↑ 聚焦最后一项；↑/↓/Home/End 移动；Enter/Space 选择；Esc 关闭并把焦点还给按钮；Tab 或点击外部关闭。`variant` 为 secondary / ghost；`iconOnly` 只显示图标（label 仍是视觉隐藏的名称，需同时给 `tooltip`）；`badge` 显示计数；带 `checked` 的项是 `menuitemradio`，选中项有对勾 |
| `Table`、`TableRow` | 凹陷表头、行分隔线、悬停；`TableRow selected`（灰底、字重 500、左侧 2 px 竖条、`aria-current`）；`ds-num` 右对齐等宽数字；外层容器横向滚动 |
| `ThemePicker` | 跟随系统 / 浅色 / 深色 |
| `ReorderList` | 从 `@/components/ds/ReorderList` 引入（尽量用 `next/dynamic` 懒加载）：拖手柄，或让手柄获得焦点后按 ↑/↓/Home/End；每次移动都会播报，焦点留在被移动项的手柄上；另导出 `moveItem`、`DS_SPRING`、`dsLayoutTransition` |

## 全局框架（第 2 阶段）

`src/app/layout.tsx` 在每个页面外层挂上 `AppFrame`（`src/components/shell/app-frame.tsx`）：

| 部分 | 作用 |
| --- | --- |
| `ShellProvider`（`shell-context.tsx`） | 主题偏好（只有一个实例，作用到 `<html>`）；命令面板和快捷键总表是否打开；全局快捷键（`global-keys.ts`） |
| `ToastProvider` | 右下角的 Toast 区域，一个 polite、一个 assertive 的 live 区域。下面任何组件都可以用 `@/components/ds` 的 `useToast().show({...})` |
| `ConfirmProvider`（`confirm.tsx`） | 根部唯一的确认对话框。`const confirm = useConfirmAction(); if (!(await confirm({ title, confirmLabel, tone }))) return;`。“取消”、Esc、点遮罩都返回 false，与 `window.confirm` 的“取消”一致；在 provider 之外总是返回 false。原生模态 `<dialog>` 打开时（训练池的推送对话框）页面其余部分是 inert 的，所以问题渲染在那个对话框里面 |
| 顶栏（`levi-header.tsx`） | 高 56 px，不透明的 `--ds-bg` 加分隔线（顶栏还不随页面滚动固定，半透明材质只会透出旧页面的深色背景；改成粘性后再用 `ds-material`）。字标；页面导航（显示实时评测时有“实时评测”、探索数据、转换与审核、提供训练池时有“训练池”、使用指南、报告），当前页标 `aria-current="page"`、字重 600、下方 2 px 指示条；右侧是搜索（打开命令面板）、作业、Agent 工作台开关、设置（账号与连接、命令面板、快捷键）、外观和语言。窄于 900 px 时页面导航移到单独一行、可横向滚动（`--levi-header-height` 变为 100 px，`.h-screen` 页面减去它） |
| 作业（`jobs-menu.tsx`、`jobs.ts`） | 正在运行的训练池作业和转换作业数量，读现有的 `/api/levi/pool/jobs` 和 `/api/levi/jobs`，首次加载、打开菜单、切回标签页时各查一次，标签页可见时每 60 秒一次（隐藏时不请求，上一个请求未返回时不再发）；菜单通向训练池和转换与审核 |
| 命令面板（`command-palette.tsx`、`commands.ts`） | macOS 上 ⌘K，其他系统 Ctrl+K，或点“搜索”。组合框加列表框：跳到页面，打开 Agent 工作台、账号与连接或快捷键总表，选择外观或语言。按两种语言的标签以及中英文关键词匹配 |
| 快捷键总表（`shortcuts-dialog.tsx`） | 按 `?` 打开（在输入框中不触发）。列出全局快捷键和页面已有的快捷键（片段查看器、标注、审核队列） |
| Agent 工作台抽屉 | `agent-workbench.tsx` 把原有内容（未改动）放进顶栏下方、右侧的非模态 `Sheet`（`levi-agent-sheet`）；旁边的页面仍可操作，左边缘仍可拖动调整宽度（宽度按浏览器保存）。顶栏的开关、命令面板和原有的窗口事件 `levi-agent-toggle` / `levi-agent-connections` 都能打开它；它用 `levi-agent-state` 报告开关状态 |

**快捷键。** 全局框架只绑定 ⌘K / Ctrl+K 和 `?`（也接受全角 `？` 和用 AltGr 打出的 `?`）。输入法组字时都不触发，`?` 在输入框中不触发，另一个模态对话框（确认框、页面自己的对话框、原生 `showModal()` 对话框）打开时也都不触发。在模态层里按的键（Tab 和 Esc 除外，由层自己处理）不会传到 `window` 上的监听，页面快捷键不会在对话框背后生效，和原生 `confirm()` 一样；确认框开着时切换页面，按“取消”作答。页面保留自己的快捷键：Space、↑/↓、J/K、Esc、Ctrl/⌘+S/Z/Y。

**加载遮罩。** `loading-component.tsx` 是 `role="status"` 加 `aria-busy="true"`，不是对话框：不拿焦点，也不困住焦点。

**Tailwind** 不扫描 `docs/`（`globals.css` 里的 `@source not "../../docs"`）：Markdown 不是界面代码，其中的词不应生成样式。

## 品牌标识

全站只有一个标识：32 单位网格上的石墨色方块，里面是几何的“L”和一个方点。`src/components/shell/brand.tsx` 里的 `LEVI_MARK` 是它唯一的定义；`<LeviMark size>` 在页面里画它（方块用 `--ds-accent`，字形用 `--ds-on-accent`，所以和主要按钮一样，浅色下黑、深色下白），`<LeviWordmark>` 再加上名称。顶栏、首页、使用指南和报告页都用它。浏览器标签图标 `src/app/icon.svg`（黑色方块，浏览器为深色主题时换成白色）、`apple-icon.png`（180 px，满版）和 `favicon.ico`（16、32、48 px）按同一组数字生成：改了标识后运行 `uv run --with pillow python scripts/brand_icons.py`；`icon.svg` 与定义不一致时 `brand.test.ts` 失败。页面不再自己画标识，酸橙绿已经去掉。

## 全局页面基础（第 5 阶段）

`globals.css` 用令牌设置页面：背景 `--ds-bg`、主文字、正文字号的系统字体、`color-scheme: light dark`（滚动条、下拉列表、日期选择器随主题变化）、低调的滚动条、选中文字底色、给顶栏留出的 `scroll-padding-top`，以及所有元素统一的石墨色焦点环（`levi.css`，`outline: 2px`，外偏 2 px；强制颜色模式下用 `Highlight`）。中文下旧的全大写、加宽字距标签按正常排版显示。

**旧名称。** 还没迁移的页面保留原来的类名；它们的颜色现在来自令牌，所以每个页面都跟随浅色和深色，不再出现旧的深绿、羊皮纸色、酸橙绿或青色。迁移时按下表替换：

| 旧名称 | 现在指向 | 迁移时改用 |
| --- | --- | --- |
| `--bg`、`--surface-0/1/2` | `--ds-bg`、`--ds-surface-sunken`、`--ds-surface-1`、`--ds-surface-2` | 对应的 `--ds-*` |
| `--text-primary/muted/faint` | `--ds-text-primary/secondary/tertiary` | 同左 |
| `--accent`、`--accent-soft`、`--accent-ring` | `--ds-accent`、12% 强调色、`--ds-focus-ring` | `--ds-accent` 只用于主要按钮、选中和进度；选中的底色用 `--ds-surface-selected` |
| `--border-subtle`、`--border-strong` | `--ds-separator`、`--ds-separator-strong` | 同左；输入框用 `--ds-border-control` |
| Tailwind `white`（`text-white`、`border-white/10`、`bg-white/5`） | `--ds-text-primary`（两套外观下都是淡线或淡底） | `--ds-separator` / `--ds-surface-hover`；视频上的文字用 `text-on-media` |
| Tailwind `slate-100…200` / `300…400` / `500…600` | 主 / 次要 / 三级文字 | 文字令牌 |
| Tailwind `slate-700` / `800` / `900` / `950` | `--ds-separator-strong` / `--ds-separator` / `--ds-surface-1` / `--ds-bg` | 同左 |
| Tailwind `cyan-*`、`lime-*` | `--ds-accent`（`cyan-200`、`600` 为 `--ds-accent-hover`） | `Button variant="primary"`、`--ds-surface-selected` |
| Tailwind `red-*`、`orange/amber/yellow-*`、`green/emerald-*`、`blue-*` | `--ds-danger`、`--ds-warning`、`--ds-success`、`--ds-info` | 带 tone 的 `Badge`、`StatusDot`；图表序列用 `--ds-data-*` |
| `.levi-workbench`、`.levi-box` | 页面框架、卡片 | `Card`，页面自己的布局 |
| `.levi-primary`、`.levi-secondary` | 画成 ds 按钮的样子 | `Button variant="primary"` / `"secondary"` |
| `.levi-input` | 画成 ds 输入框的样子 | `Field` 里的 `Input`、`Select`、`Textarea` |
| `.levi-table`、`.levi-status`、`.levi-error`、`.levi-code`、`.levi-metrics`、`.levi-eyebrow` | ds 表格、中性或状态徽章、错误提示、代码块、指标卡、分区标签 | `Table`、`Badge`、三段式错误（发生了什么、为什么、怎么办）、`ds-*` 样式的 `<pre>` |
| `.panel`、`.panel-raised` | 卡片、抬升卡片 | `Card`、`Card variant="raised"` |

`levi.css` 里仍是片段查看器、实时评测、训练池、转换与审核、Agent 工作台的页面样式；其中每个写死的颜色都按用途（表面、文字、线、强调、状态）映射到了令牌。某个类没有页面再用时，就从 `levi.css` 删除。`global-styles.test.ts` 在 `globals.css`、`home.css`、`reading.css`、`report.css` 出现颜色字面量、`levi.css` 出现黑色以外的颜色字面量，或这些文件里出现旧配色时失败。

**首页**（`/`）是工作入口：继续（这个浏览器里最近打开的片段或数据集；框架把访问记录存在 `localStorage` 的 `levi-recent` 里，不发送到任何地方）、需要你处理（`waiting_for` 为人工批准、审核或提交的 agent 任务，来自 `/api/levi/agent/v1/activity/tasks`）、正在运行（转换和训练池作业及其进度）、显示实时评测时的状态卡，以及最近的数据集（先列打开过的，再列其他已登记的）。每张卡单独加载，先显示骨架，10 秒没有回应就放弃；标签页可见时每 15 秒刷新。Hugging Face 搜索和旧的 `/?path=`、`/?dataset=` 链接保留。原来的介绍移到了使用指南。

**阅读型版式**（`reading.css`，使用指南和报告页）：单栏，最宽 760 px；左侧目录始终可见并标出当前小节（`aria-current="location"`）；正文 16/26（`--ds-text-reading-*`）；`.levi-prose` 统一 Markdown 的标题、列表、链接（主文字加下划线）、引用、代码和表格。报告页的图表通过 `useCssTokens` 使用 `--ds-data-*`（SVG 属性读不到 CSS 变量），运行中状态每 2 秒呼吸一次、减少动态效果时静止，“已更新”用全局 Toast 提示。

## 动效

| 交互 | 动效 | 减少动态效果时 |
| --- | --- | --- |
| 悬停、按下 | 颜色变化 120 ms；按下 `scale(0.98)` | 只变颜色 |
| 切标签、分段、逐帧 | 无动画，立即切换 | — |
| 菜单、提示 | 淡入 + 上移 4 px，200 ms | 无 |
| 对话框 | 遮罩淡入，对话框 `scale(0.97→1)` + 淡入，200 ms | 无 |
| 抽屉 | 从所在侧滑入，320 ms 弹性 | 无 |
| Toast | 上移 8 px + 淡入，200 ms | 无 |
| 进度 | 宽度 200 ms 过渡；不确定进度条来回移动 | 静止，显示“进行中” |
| 运行中状态 | 每 2 秒一次透明度呼吸 | 静止 |
| 拖拽与重排（Motion） | 抓起时 `scale(1.02)` + 阴影加深，弹性到位 | 直接到位 |

`prefers-reduced-motion: reduce` 时，`--ds-dur-base`、`--ds-dur-slow`（及退出时长）变为 0，`--ds-ease-spring` 变为 `linear`，所有位移令牌归零；旋转图标、不确定进度条、呼吸状态点都停下。祖先元素上的 `data-motion="reduce"` 有同样效果（用于预览和测试）。渲染内容会变的组件用 `usePrefersReducedMotion()`；`ReorderList` 用 Motion 的 `useReducedMotion` 和 `MotionConfig reducedMotion="user"`。`ReducedMotionScope reduce` 可对一个子树强制走减少动态效果的路径（样张页的预览开关就是这样做的；同时在元素上加 `data-motion="reduce"`，让 CSS 也跟着变）。第 1 阶段没有退出动画，关闭时直接消失。

禁止无限循环的装饰动画、视差、滚动劫持、超过 400 ms 的动画和阻塞操作的动画。

**Motion**（`motion` 包，即原 Framer Motion，MIT 许可）只用在 `ReorderList` 里，gzip 后约 45 KB，只有引入该组件的页面才会加载。

## 可访问性

- 焦点环：2 px `--ds-focus-ring`，外偏 2 px（每个可交互组件都带 `ds-focus` 类）；Windows 高对比模式下用 `Highlight`。
- 每个弹出层都有焦点陷阱（监听挂在 `document` 上，焦点跑到哪里都管得住），Esc 关闭，关闭后焦点返回；菜单和标签页遵循 ARIA 模式；Toast 用一个 polite 和一个 assertive 的 live 区域。
- 可点区域至少 24 × 24 px。

## 样张页

`/design` 并排展示浅色和深色下的全部令牌和组件（`?only=light` 或 `?only=dark` 只显示一套，`?motion=reduce` 以减少动态效果开始）。`next dev` 下可访问；生产服务（`next start`、`levi serve`）里，除非环境里设了 `LEVI_DESIGN_PAGE=1`（`src/lib/design/gate.ts`），`src/middleware.ts` 会在路由之前直接返回纯文本 404，不发送页面的元数据和样式。导航里没有它的入口。

## 测试

`global-styles.test.ts` 检查页面基础和旧样式（见上），`brand.test.ts` 检查标识，`recent.test.ts` 和 `home-data.test.ts` 检查首页的数据；ESLint 的十六进制颜色规则也覆盖首页、使用指南、报告页和 `src/lib/design/`。

`bun test` 在 DOM（happy-dom，开发依赖）里运行组件测试。组件测试先引入 `./dom` 并调用 `setupDom()`，其中有 `render`、`press`、`click`、`focus`、`dropFocus`、`fire`、`flush`、`mockMatchMedia`。每个这样的测试文件结束后会移除 DOM 全局对象，其他测试仍在没有 DOM 的环境里运行。`tokens-contrast.test.ts` 还会在组件或样张页的 TSX 里发现颜色字面量（十六进制、`rgb()`、`hsl()`）或 Tailwind 任意值时报错。
