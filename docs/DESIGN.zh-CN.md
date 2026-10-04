# 设计系统

[English](DESIGN.md)

LEVI 界面分阶段重构：石墨强调色，浅色和深色两套外观、默认跟随系统，只用系统字体，图标用 Lucide，Motion 只用于拖拽和列表重排。第 1 阶段（本文）加入设计令牌、主题偏好和一组基础组件。**现有页面还没有用它们**：`globals.css`、`levi.css`、`annotations-skin.css`、`report.css` 没有改动，现有页面的显示与之前完全相同。页面在后续阶段逐个迁移。

| 内容 | 位置 |
| --- | --- |
| 令牌（CSS 变量 `--ds-*`） | `src/styles/tokens.css` |
| 组件样式（类名 `ds-*`） | `src/styles/ds.css` |
| 组件 | `src/components/ds/`（`index.ts` 导出除 `ReorderList` 外的全部组件） |
| 主题偏好 | `src/lib/design/theme.ts` |
| 减少动态效果的工具、时长、断点 | `src/lib/design/motion.ts` |
| 样张页开关 | `src/lib/design/gate.ts`、`src/middleware.ts` |
| 样张页（仅开发用） | `/design`（`src/app/design/`） |
| 测试 | `src/components/ds/__tests__/`、`src/lib/design/__tests__/` |

## 新代码的规则

- **禁止硬编码颜色。** 用语义令牌（`var(--ds-text-secondary)`、`var(--ds-surface-1)`）或 `ds-*` 类。新的 CSS 和 TSX 里不写十六进制、`rgb()`、`hsl()`，也不写 Tailwind 任意值；`ds.css`、样张页 CSS、组件和样张页的 TSX 里出现就会让测试失败。数据配色（时间片段、掩码、图表序列）是单独一套，第 4 阶段定义。
- **组件里只用语义令牌。** 原始灰阶 `--ds-gray-l-*`、`--ds-gray-d-*` 只用来定义语义令牌。
- **每屏一个主要按钮。** 强调色 A“石墨”是最深的灰，只用于主要按钮、焦点环、选中态和进度。正文中的链接用主文字色加下划线。
- **状态不只靠颜色。** 状态色（成功、警告、错误、信息）只出现在徽章、状态点、Toast 和行内提示上，并且总带图标形状和文字（`Badge`、`StatusDot`）。
- **最小字号 12 px**（`--ds-text-caption-size`）；字重只用 400、500、600。
- **图标**只用 Lucide，经 `Icon` 引入（16 px 配描边 1.75；20、24 px 配 1.5）。装饰性图标 `aria-hidden`；承载含义的图标给 `label`。
- **不用 `title=` 做提示。** 用 `Tooltip`（悬停和键盘焦点都会出现）；只有图标的按钮用 `IconButton`，它必须有 `label`（即 `aria-label` 和提示文字）。
- **不用 `window.confirm`。** 只有不可逆的操作才用 `ConfirmDialog` 或 `useConfirm()`；预期内、可撤销的删除直接删除，并在 Toast 里给“撤销”。
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

`useThemePreference()` 返回 `{ preference, resolved, setPreference }`：取值 `"system" | "light" | "dark"`，存在 `localStorage` 的 `levi-theme` 键下（`"system"` 会删除该键）。读写都经过不会抛异常的 `browserStorage`；存储不可用时为“跟随系统”。它跟随系统外观的变化和其他标签页的修改，但**不会改 `<html>`**：调用方用 `applyTheme(element, preference)` 决定主题作用在哪里。第 2 阶段页面改用令牌之后才把它作用到 `<html>`，在此之前现有页面保持深色。`ThemePicker` 是“跟随系统 / 浅色 / 深色”的切换控件。

给容器加 `ds-root` 类，它就使用设计系统的字体、文字色和背景，`color-scheme` 也随主题变化。

## 组件

从 `@/components/ds` 引入；样式需要引入一次 `@/styles/tokens.css` 和 `@/styles/ds.css`（样张页已引入；第 2 阶段移到根布局）。

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
| `Dialog`、`Sheet` | 模态（`aria-modal`），以标题命名；焦点移入，Tab 在内部循环，焦点落到外面（点了遮罩、别处调用了 `focus()`）会被拉回，Esc 和点遮罩关闭（`closeOnScrim={false}` 时点遮罩不关），关闭后焦点回到打开它的元素；嵌套时只有最上层处理 Tab 和 Esc，内部菜单先处理自己的 Esc；`container` 可渲染到别的元素（portal）；`Sheet side` 为 right / left / bottom |
| `ConfirmDialog`、`useConfirm` | `role="alertdialog"`；标题写清动作和对象，`confirmLabel` 用动词；`tone="danger"` 时默认焦点在“取消”；`const { confirm, dialog } = useConfirm()` 后 `await confirm({...})`，可取代 `window.confirm` |
| `ToastProvider`、`useToast` | 右下角；`show({ title, description, tone, action, duration })`；普通通知走 polite 区域，最多 3 条，4 秒后消失（悬停或聚焦时暂停）；带操作按钮的 Toast 默认不自动消失（除非给 `duration`）；错误（`danger`）走 assertive 区域，不会被新通知挤掉，直到手动关闭；在 provider 之外 `show` 什么也不做 |
| `Skeleton`、`SkeletonText` | 静态灰块（不扫光），对辅助技术隐藏；正在加载的区域标 `aria-busy="true"` |
| `Progress`、`Spinner` | `Progress value={n}` 为确定进度；`value={null}` 为不确定进度（一段来回移动的条，减少动态效果时静止并显示“进行中”）；`Spinner` 是 `role="status"`；两者都不是对话框。局限：每个 `Spinner` 自己是一个 live 区域，而与文字一起插入的 live 区域并非所有读屏软件都会播报；必须让人听到的结果，请保持一个常驻的状态区域只改其文字，或用 Toast |
| `EmptyState` | 图标、一句说明、下一步操作按钮（`action`、`secondaryAction`） |
| `Tabs` | ARIA 标签页：一个 Tab 停靠点，←/→、Home/End，跳过禁用项；切换立即生效；只有选中的标签写 `aria-controls`；`value` 找不到可用项时选中第一个未禁用的标签 |
| `SegmentedControl` | 画成拼接按钮的单选组；方向键移动并选中 |
| `Menu` | 菜单按钮：Enter/Space/↓ 打开并聚焦第一项，↑ 聚焦最后一项；↑/↓/Home/End 移动；Enter/Space 选择；Esc 关闭并把焦点还给按钮；Tab 或点击外部关闭 |
| `Table`、`TableRow` | 凹陷表头、行分隔线、悬停；`TableRow selected`（灰底、字重 500、左侧 2 px 竖条、`aria-current`）；`ds-num` 右对齐等宽数字；外层容器横向滚动 |
| `ThemePicker` | 跟随系统 / 浅色 / 深色 |
| `ReorderList` | 从 `@/components/ds/ReorderList` 引入（尽量用 `next/dynamic` 懒加载）：拖手柄，或让手柄获得焦点后按 ↑/↓/Home/End；每次移动都会播报，焦点留在被移动项的手柄上；另导出 `moveItem`、`DS_SPRING`、`dsLayoutTransition` |

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

`bun test` 在 DOM（happy-dom，开发依赖）里运行组件测试。组件测试先引入 `./dom` 并调用 `setupDom()`，其中有 `render`、`press`、`click`、`focus`、`dropFocus`、`fire`、`flush`、`mockMatchMedia`。每个这样的测试文件结束后会移除 DOM 全局对象，其他测试仍在没有 DOM 的环境里运行。`tokens-contrast.test.ts` 还会在组件或样张页的 TSX 里发现颜色字面量（十六进制、`rgb()`、`hsl()`）或 Tailwind 任意值时报错。
