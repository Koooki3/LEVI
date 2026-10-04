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
| 样张页（仅开发用） | `/design`（`src/app/design/`） |
| 测试 | `src/components/ds/__tests__/`、`src/lib/design/__tests__/` |

## 新代码的规则

- **禁止硬编码颜色。** 用语义令牌（`var(--ds-text-secondary)`、`var(--ds-surface-1)`）或 `ds-*` 类。新的 CSS 和 TSX 里不写十六进制、`rgb()`、`hsl()`；`ds.css` 或样张页的 CSS 里出现就会让测试失败。数据配色（时间片段、掩码、图表序列）是单独一套，第 4 阶段定义。
- **组件里只用语义令牌。** 原始灰阶 `--ds-gray-l-*`、`--ds-gray-d-*` 只用来定义语义令牌。
- **每屏一个主要按钮。** 强调色 A“石墨”是最深的灰，只用于主要按钮、焦点环、选中态和进度。正文中的链接用主文字色加下划线。
- **状态不只靠颜色。** 状态色（成功、警告、错误、信息）只出现在徽章、状态点、Toast 和行内提示上，并且总带图标形状和文字（`Badge`、`StatusDot`）。
- **最小字号 12 px**（`--ds-text-caption-size`）；字重只用 400、500、600。
- **图标**只用 Lucide，经 `Icon` 引入（16 px 配描边 1.75；20、24 px 配 1.5）。装饰性图标 `aria-hidden`；承载含义的图标给 `label`。
- **不用 `title=` 做提示。** 用 `Tooltip`（悬停和键盘焦点都会出现）；只有图标的按钮用 `IconButton`，它必须有 `label`（即 `aria-label` 和提示文字）。
- **不用 `window.confirm`。** 只有不可逆的操作才用 `ConfirmDialog` 或 `useConfirm()`；预期内、可撤销的删除直接删除，并在 Toast 里给“撤销”。
- **文案**走语言目录（`useLocale().t`，`en.json` 和 `zh.json` 同时加）。组件的默认文字（Close、Cancel、Loading、In progress、Dismiss notification、Notifications、Move、Theme、System、Light、Dark）已经在目录里。

## 令牌

全部令牌是 `:root` 上的 CSS 自定义属性，统一以 `--ds-` 开头，不会与旧的 `--bg`、`--accent`、`--surface-*` 冲突。分组与英文版的表相同：中性灰阶（浅色 12 级、深色 13 级）、表面（背景、凹陷区、卡片、抬升层、悬停、选中、输入框底、骨架、始终为黑的媒体底、遮罩）、线（分隔线、控件边界）、文字（主、次、三级及其在凹陷/悬停/选中/抬升表面上的替代、禁用、图标）、强调色 A、状态色、阴影与材质、字体与字号层级、圆角 5 档、4 px 基数的间距、控件高度、动效时长与缓动、z-index 层级。断点（640、900、1200、1440 px）是 `src/lib/design/motion.ts` 里的常量 `BREAKPOINT`，因为媒体查询读不到 CSS 变量。

**对比度。** 两套主题下，文字配对都达到 4.5:1，控件边界、焦点环、选中竖条和进度达到 3:1；`tokens-contrast.test.ts` 直接从 `tokens.css` 读取数值，用 WCAG 公式计算。提案判为不达标的配对用专门的令牌替代：浅色凹陷区和悬停行里的三级文字、深色抬升层里的三级文字，以及深色抬升层里的控件边界。在 `Card variant="sunken"`、菜单、对话框、抽屉、提示、Toast，以及悬停或选中的表格行里，这些替换自动生效（`ds-on-sunken`、`ds-on-raised`）；其他画在这些表面上的容器，自己加上 `ds-on-sunken` 或 `ds-on-raised` 类。

**层次。** 相邻表面差一到两级灰；卡片始终有 1 px 分隔线描边（浅色卡片与背景只有 1.09:1）。阴影用于浅色；深色用更亮的表面加内描边。玻璃材质（`ds-material`）只给浮在内容上的功能层（顶栏、浮动工具条）；在 `prefers-reduced-transparency` 或 `prefers-contrast: more` 下变为不透明。

## 主题

默认浅色。系统为深色（`prefers-color-scheme: dark`）且祖先元素没有 `data-theme="light"` 时用深色；`data-theme="dark"` 下总是深色。`data-theme` 可以放在任何元素上，单独给一个子树换主题（样张页就是这样并排显示两套）。`prefers-contrast: more` 时，分隔线、控件边界和次要文字各提高一级。

`useThemePreference()` 返回 `{ preference, resolved, setPreference }`：取值 `"system" | "light" | "dark"`，存在 `localStorage` 的 `levi-theme` 键下（`"system"` 会删除该键）。读写都经过不会抛异常的 `browserStorage`；存储不可用时为“跟随系统”。它跟随系统外观的变化和其他标签页的修改，但**不会改 `<html>`**：调用方用 `applyTheme(element, preference)` 决定主题作用在哪里。第 2 阶段页面改用令牌之后才把它作用到 `<html>`，在此之前现有页面保持深色。`ThemePicker` 是“跟随系统 / 浅色 / 深色”的切换控件。

给容器加 `ds-root` 类，它就使用设计系统的字体、文字色和背景，`color-scheme` 也随主题变化。

## 组件

从 `@/components/ds` 引入；样式需要引入一次 `@/styles/tokens.css` 和 `@/styles/ds.css`（样张页已引入；第 2 阶段移到根布局）。组件清单和各自的键盘行为见英文版的表：`Button`、`IconButton`、`Icon`、`Tooltip`、`Field`/`Input`/`Textarea`/`Select`、`Checkbox`/`Radio`/`RadioGroup`/`Switch`、`Badge`/`StatusDot`/`Tag`、`Card`/`Divider`/`Kbd`、`Dialog`/`Sheet`、`ConfirmDialog`/`useConfirm`、`ToastProvider`/`useToast`、`Skeleton`/`SkeletonText`、`Progress`/`Spinner`、`EmptyState`、`Tabs`、`SegmentedControl`、`Menu`、`Table`/`TableRow`、`ThemePicker`、`ReorderList`。

要点：所有弹出层（对话框、抽屉、确认框）有焦点陷阱，Esc 关闭，关闭后焦点回到打开它的元素；危险确认框默认焦点在“取消”；Toast 成功类 4 秒后消失（悬停或聚焦时暂停），错误类不自动消失并走 assertive 区域；`Progress` 和 `Spinner` 都不是对话框；`ReorderList` 可以拖手柄，也可以让手柄获得焦点后按 ↑/↓/Home/End，每次移动都会播报。

## 动效

时长 120–320 ms，不循环、不阻塞、可打断；切标签、逐帧没有动画。`prefers-reduced-motion: reduce` 时，`--ds-dur-base`、`--ds-dur-slow`（及退出时长）变为 0，`--ds-ease-spring` 变为 `linear`，位移和缩放令牌归零；旋转图标、不确定进度条、呼吸状态点都停下，不确定进度改为显示“进行中”；`ReorderList` 直接到位。祖先元素上的 `data-motion="reduce"` 有同样效果（用于预览和测试）。第 1 阶段没有退出动画。

**Motion**（`motion` 包，即原 Framer Motion，MIT 许可）只用在 `ReorderList` 里，gzip 后约 45 KB，只有引入该组件的页面才会加载。

## 样张页

`/design` 并排展示浅色和深色下的全部令牌和组件（`?only=light` 或 `?only=dark` 只显示一套，`?motion=reduce` 以减少动态效果开始）。`next dev` 下可访问；生产服务（`next start`、`levi serve`）除非环境里设了 `LEVI_DESIGN_PAGE=1`，否则返回 404。导航里没有它的入口。

## 测试

`bun test` 在 DOM（happy-dom，开发依赖）里运行组件测试。组件测试先引入 `./dom` 并调用 `setupDom()`，其中有 `render`、`press`、`click`、`focus`、`fire`、`flush`、`mockMatchMedia`。每个这样的测试文件结束后会移除 DOM 全局对象，其他测试仍在没有 DOM 的环境里运行。
