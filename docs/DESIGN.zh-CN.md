# 设计系统

[English](DESIGN.md)

这份文档说明 LEVI 界面现在的构成：石墨强调色，浅色和深色两套外观（没有人选择时跟随系统），只用系统字体，图标用 Lucide，Motion 只用于拖拽和列表重排。每个路由的颜色都来自 `--ds-*` 令牌和定义它们的样式表；旧样式表 `levi.css`、旧的 CSS 变量（`--bg`、`--accent`、`--surface-*`）和被映射过的 Tailwind 色板都已删除。文档保留各阶段的小标题（更新日志里链接到它们）；哪些部分已经用 `ds-*` 组件、哪些仍是在令牌之上拼 Tailwind 工具类，见[迁移状态](#迁移状态)。

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
| 页面基础（第 5 阶段） | `src/app/globals.css`（除上面三份外唯一的全局样式表） |
| 标签页标题 | `src/components/shell/route-title.tsx` |
| 首页、使用指南、报告页（第 5 阶段） | `src/components/home/`、`src/styles/home.css`；`src/app/guide/`、`src/styles/reading.css`；`src/components/report/`、`src/app/report/report.css` |
| 代码里取令牌值（canvas、WebGL） | `src/lib/design/css-tokens.ts`（`useCssTokens`） |
| 颜色 lint | `eslint.config.mjs`（`HEX_EXCEPTIONS`）、`src/__tests__/eslint-hex.test.ts` |
| 语言目录 | `src/i18n/en.json`、`src/i18n/zh.json`，由 `src/i18n/__tests__/catalog.test.ts` 检查 |
| 测试 | `src/components/ds/__tests__/`、`src/lib/design/__tests__/`、`src/components/shell/__tests__/` |

## 新代码的规则

- **禁止硬编码颜色。** 用语义令牌（`var(--ds-text-secondary)`、`var(--ds-surface-1)`）或 `ds-*` 类。CSS 和 TSX 里不写十六进制、`rgb()`、`hsl()`，也不写 Tailwind 任意颜色值。三道检查：ESLint（`no-restricted-syntax`）在 **所有** `src/**/*.{ts,tsx}` 里拒绝十六进制颜色，以及带字面数字的 `rgb()` / `hsl()`，只有 `eslint.config.mjs` 里 `HEX_EXCEPTIONS` 列出的文件除外（URDF 查看器的机器人材质、给 canvas 和 WebGL 用的 `data-palette.ts`、测试；每一项都在注释里写明理由，新增例外必须说明令牌为什么不能用）。颜色指字符串开头或空格、`(`、`,`、`:` 之后的 `#` 加 3、4、6 或 8 位十六进制数字；`href`、`to`、`id`、`htmlFor` 的值是链接，不检查。测试（`global-styles.test.ts`、`tokens-contrast.test.ts`、各页面样式测试）在 `globals.css`、`ds.css`、`shell.css`、`home.css`、`reading.css`、`report.css`、`pages.css`、`agent-content.css`、`viewer.css`、`annotations.css` 出现颜色字面量时失败（`viewer.css` 里的数据配色是 `tokens.css` 的镜像，见[数据色](#数据色)），在 `src/` 任何地方出现 Tailwind 色板类（`text-white`、`bg-slate-800`、`border-cyan-400`……）时也失败。
- **组件里只用语义令牌。** 原始灰阶 `--ds-gray-l-*`、`--ds-gray-d-*` 只用来定义语义令牌。
- **每屏一个主要按钮。** 强调色 A“石墨”是最深的灰，只用于主要按钮、焦点环、选中态和进度。正文中的链接用主文字色加下划线。
- **状态不只靠颜色。** 状态色（成功、警告、错误、信息）只出现在徽章、状态点、Toast 和行内提示上，并且总带图标形状和文字（`Badge`、`StatusDot`）。
- **最小字号 12 px**（`--ds-text-caption-size`）；字重只用 400、500、600。
- **图标**只用 Lucide，经 `Icon` 引入（16 px 配描边 1.75；20、24 px 配 1.5）。装饰性图标 `aria-hidden`；承载含义的图标给 `label`。
- **`title=` 不是界面。** 控件的名称、图标的含义、按钮被禁用的原因，以及一切需要人据此行动的信息，都用可见的文字、`Tooltip`（悬停和键盘焦点都会出现）或 `aria-describedby` 关联；只有图标的按钮用 `IconButton`，它必须有 `label`（即 `aria-label` 和提示文字）。原生 `title` 只允许作为补充，加在已经显示了关键内容的非交互文字上：被截断的表格单元格的全文、精确时间、id 或路径。
- **不用 `window.confirm`。** 通过 `useConfirmAction()`（`src/components/shell/confirm.tsx`）询问，`src/` 里出现 `window.confirm` 会让测试失败；只有不可逆的操作才询问；预期内、可撤销的删除直接删除，并在 Toast 里给“撤销”。
- **文案**走语言目录（`useLocale().t`，`en.json` 和 `zh.json` 同时加）。组件的默认文字（Close、Cancel、Loading、In progress、Dismiss notification、Notifications、Move、Theme、System、Light、Dark）已经在目录里。

## 令牌

全部令牌是 `:root` 上的 CSS 自定义属性，统一以 `--ds-` 开头。（旧的 `--bg`、`--accent`、`--surface-*` 变量已不存在。）

| 分组 | 令牌 |
| --- | --- |
| 中性灰阶 | 浅色 `--ds-gray-l-0…11`，深色 `--ds-gray-d-0…12`（略带冷调的中性灰，提案 §4.1） |
| 表面 | `--ds-bg`、`--ds-bg-reading`、`--ds-surface-sunken`、`--ds-surface-1`（卡片）、`--ds-surface-2`（抬升：菜单、对话框）、`--ds-surface-hover`、`--ds-surface-hover-on-raised`、`--ds-surface-selected`、`--ds-field-bg`、`--ds-skeleton`、`--ds-media-bg`（两套都是黑）、`--ds-scrim` |
| 线 | `--ds-separator`、`--ds-separator-strong`、`--ds-border-control`、`--ds-border-control-on-raised` |
| 文字 | `--ds-text-primary`、`--ds-text-secondary`、`--ds-text-tertiary`、`--ds-text-tertiary-on-sunken`、`-on-hover`、`-on-selected`、`-on-raised`、`--ds-text-placeholder`（输入框的占位文字：普通的三级文字色阶，抬升层不重映射它，所以对话框里的占位文字仍比已输入的值浅）、`--ds-text-disabled`、`--ds-icon` |
| 强调色 A | `--ds-accent`、`--ds-accent-hover`、`--ds-on-accent`、`--ds-focus-ring`、`--ds-selected-indicator`、`--ds-progress`、`--ds-progress-track` |
| 状态 | `--ds-success`、`--ds-warning`、`--ds-danger`、`--ds-info`，各带 `-bg`；`--ds-on-danger` |
| 数据 | `--ds-data-1…8`（蓝、橙、青绿、黄、品红、绿、紫、红，顺序固定）：序列、时间片段和类别颜色，浅深两套。用 dataviz 配色检查校验过：相邻颜色在色觉障碍下（ΔE ≥ 8.4）和正常视觉下（ΔE ≥ 19.3）都能分开；详见[数据色](#数据色)。不用于文字和界面状态 |
| 媒体 | `--ds-media-bg`（黑）、`--ds-on-media`、`--ds-on-media-secondary`（视频和图像上的白色文字，两套外观相同；Tailwind `text-on-media`）、`--ds-media-scrim` |
| 层次与材质 | `--ds-shadow-1…3`、`--ds-ring-raised`（深色）、`--ds-material-bar`、`--ds-material-filter` |
| 字体 | `--ds-font-sans`、`--ds-font-mono`；`--ds-text-{display,title-1,title-2,title-3,body,reading,callout,caption}-{size,line}`；`--ds-weight-{regular,medium,semibold}`；`--ds-tracking-{title,display}` |
| 形状与间距 | `--ds-radius-{xs,sm,md,lg,full}`（4、6、10、14、999 px）；`--ds-space-{0-5,1,2,3,4,5,6,8,10,12,16}`（4 px 基数）；`--ds-control-{sm,md,lg}`（28、32、40 px）；`--ds-hit-min`；`--ds-icon-{sm,md,lg}` |
| 动效 | `--ds-dur-{instant,fast,base,base-exit,slow,slow-exit}`（0、120、200、140、320、220 ms）；`--ds-ease-{standard,exit,spring}`；`--ds-motion-{shift,toast-shift,dialog-scale,press-scale,drawer-shift}` |
| 层级 | `--ds-z-{base,sticky,dock,popover,overlay,dialog,toast}`（0–60） |

断点（640、900、1200、1440 px）是 `src/lib/design/motion.ts` 里的常量 `BREAKPOINT`，因为媒体查询读不到 CSS 变量。

**对比度。** 两套主题下，文字配对都达到 4.5:1，控件边界、焦点环、选中竖条和进度达到 3:1；`tokens-contrast.test.ts` 直接从 `tokens.css` 读取数值，用 WCAG 公式计算。提案判为不达标的配对用专门的令牌替代：凹陷区、悬停行、选中行和抬升层里的三级文字，深色抬升层里的控件边界，以及深色抬升层里的悬停底（gray-6，比抬升表面 gray-5 更亮）。有两个状态色与提案不同，以便在悬停底上也达标：浅色“信息”`#3a6693`（原 `#3d6a99`，在悬停底上 4.41:1），深色“错误”`#ee8f80`（原 `#e8806f`，在抬升层悬停底上 4.19:1）。状态色对卡片、抬升层、输入框底、悬停底、抬升层悬停底都做检查，危险按钮悬停色对其文字也做检查。在 `Card variant="sunken"`、菜单、对话框、抽屉、提示、Toast，以及悬停或选中的表格行里，这些替换自动生效（`ds-on-sunken`、`ds-on-raised`）；其他画在这些表面上的容器，自己加上 `ds-on-sunken` 或 `ds-on-raised` 类。

**层次。** 相邻表面差一到两级灰；卡片始终有 1 px 分隔线描边（浅色卡片与背景只有 1.09:1）。阴影用于浅色；深色用更亮的表面加内描边。玻璃材质（`ds-material`）只给浮在内容上的功能层（顶栏、浮动工具条）；在 `prefers-reduced-transparency` 或 `prefers-contrast: more` 下变为不透明。

### 数据色

数据色不是界面色。唯一来源是 `tokens.css`：8 个分类色按固定顺序使用（`--ds-data-1…8`），浅深各有一级。用 dataviz 配色检查校验过：相邻颜色在色觉障碍下（ΔE ≥ 8.4）和正常视觉下（ΔE ≥ 19.3）都能分开，深色各级对深色页面底和卡片都至少 3:1（有测试）。其余一切都从这里取：图表序列（`seriesColor(i)`，报告页图表经 `useCssTokens`）、标注样式（`annotations.css` 里的 `--style-subtask` … `--style-memory`）、RECAP 优势（`--dv-positive`、`--dv-negative`）、时间片段和视频上的标签。`viewer.css` 把它们镜像成 `--dv-1…8`（令牌的别名）；画布和三维场景读不到 CSS 变量，用 `data-palette.ts` 里的 `DATA_ON_MEDIA`（深色阶的数值，有测试保证与 `viewer.css` 一致），这是查看器里唯一允许出现 hex 的文件。

**有意偏差：三个浅色数据色低于 3:1。** 浅色主题下 `--ds-data-3`、`--ds-data-4`、`--ds-data-5`（青绿、黄、品红）对白底不到 3:1（约 2.8、2.2、2.7），低于提案对图形要求的 3:1。这是有意的：把它们压暗到 3:1，相邻颜色在色觉障碍下就分不开了，而让所有人都能区分序列优先（相邻色觉障碍 ΔE ≥ 8.4）。约束：数据色从不单独表达含义。每个有颜色的标记旁边都有文字（图例、轨道名，或用文字色写的标签），数据色从不用于文字；测试只允许这三个在浅色下低到 2:1。状态（通过/警告/失败、成功/失败）用状态色令牌，并配图标和文字，不用数据色。

## 主题

样式表的基础是浅色；系统为深色（`prefers-color-scheme: dark`）且祖先元素没有 `data-theme="light"` 时用深色；`data-theme="dark"` 下总是深色。`data-theme` 可以放在任何元素上，单独给一个子树换主题（样张页就是这样并排显示两套）。`prefers-contrast: more` 时，分隔线、控件边界和次要文字各提高一级。

`useThemePreference()` 返回 `{ preference, resolved, setPreference }`：取值 `"system" | "light" | "dark"`，存在 `localStorage` 的 `levi-theme` 键下。**默认值是 `"system"`**：从没选过的人跟随系统，存储不可用时也一样（`src/lib/design/theme.ts` 里的 `THEME_DEFAULT_PREFERENCE`，`theme-boot.ts` 里重复了一份，有测试保证两者相同）。任何显式选择（包括“跟随系统”）都会保存，所以默认值以后改了，这个选择仍然有效。读写都经过不会抛异常的 `browserStorage`。这个 hook 跟随系统外观的变化和其他标签页的修改；它本身不改 `<html>`，`applyTheme(element, preference)` 才改。全局框架（`ShellProvider`）持有唯一的偏好并把它作用到 `<html>`；根布局 `<head>` 里的一小段脚本（`theme-boot.ts`）在首次绘制前应用已存的浅色/深色选择（已存“跟随系统”或没有存储值时不写），顶栏不会先闪一下另一套外观。原生控件跟随主题：`<html>` 的 `color-scheme` 为 `light dark`，有 `data-theme` 时按它设置。`ThemePicker` 是“跟随系统 / 浅色 / 深色”的切换控件；顶栏用一个有同样三个选项的菜单。

给容器加 `ds-root` 类，它就使用设计系统的字体、文字色和背景，`color-scheme` 也随主题变化。

## 组件

从 `@/components/ds` 引入。根布局为所有页面引入一次 `@/styles/tokens.css`、`@/styles/ds.css` 和 `@/styles/shell.css`。

| 组件 | 说明 |
| --- | --- |
| `Button` | `variant` 主要 / 次要（默认）/ 幽灵 / 危险；`size` sm / md / lg（28 / 32 / 40 px）；`loading`（旋转图标、`aria-busy`、忽略点击、保留文字）；`icon`、`iconEnd`；默认 `type="button"` |
| `IconButton` | `label` 必填（无障碍名称和提示文字）；`shortcut`；切换按钮用 `pressed`（`aria-pressed`） |
| `Icon` | Lucide 图标，`size` sm / md / lg；给 `label` 时为 `role="img"` |
| `Tooltip` | 悬停 0.5 秒后出现，键盘获得焦点时立即出现：指 Tab、方向键、Home、End 或翻页键之后 0.6 秒内的焦点。点击不出现，程序设置的焦点也不出现（对话框或抽屉打开时把焦点放到第一个控件上，通常是按 Enter 打开之后），否则会弹出一个在弹层边缘被裁掉的提示。Esc、指针移开、失去焦点时关闭；给触发元素设 `aria-describedby` |
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
| 顶栏（`levi-header.tsx`） | 高 56 px，**吸顶**：页面滚动时留在顶部，用顶栏材质绘制（`--ds-material-bar` 加 `--ds-material-filter`：半透明加模糊；在 `prefers-reduced-transparency` 或 `prefers-contrast: more` 下不透明），带分隔线。字标（`brand.tsx` 的 `LeviWordmark`）；页面导航（实时评测，始终显示：有实时服务时带服务状态和需要人处理的数量，否则是指向说明如何启动的页面的普通链接；探索数据、转换与审核、提供训练池时有“训练池”、使用指南、报告），当前页标 `aria-current="page"`、字重 600、下方 2 px 指示条；右侧是搜索（打开命令面板）、作业、Agent 工作台开关、设置（账号与连接、命令面板、快捷键、动效）、外观和语言。窄于 900 px 时页面导航移到单独一行，顶栏高 100 px（`--levi-header-height`），并随页面滚走而不吸顶（否则会遮住手机屏幕的一大块） |
| 顶栏之下（`shell.css`、`globals.css`） | `--levi-header-height` 是顶栏高度（整屏高度的页面要减去它）；`--levi-sticky-top` 是滚动时顶栏盖住的顶部高度（宽屏同为 56 px，窄于 900 px 为 0）。`scroll-padding-top`、阅读版式的锚点（`scroll-margin-top`）和目录（`top`），以及自己吸顶的页面部件（实时评测横幅、训练池筛选面板、样张页的标题）都从它下面开始。吸附在窗口顶部的页面部件用 `top: calc(var(--levi-sticky-top) + …)`；吸附在自己滚动框里的（表头、标注列表）不用 |
| 标签页标题（`route-title.tsx`） | 根布局的元数据只有名称（“LEVI”，描述为中英双语）；`RouteTitle` 把 `document.title` 设为当前语言下的页面名加名称：“Explore · LEVI”、“训练池 · LEVI”、“Episode viewer · lerobot/aloha_static_coffee · LEVI”。页面和名称见 `routePageName`（首页、使用指南、报告、探索数据、转换与审核、训练池、实时评测、片段查看器、样张页） |
| 作业（`jobs-menu.tsx`、`jobs.ts`） | 正在运行的训练池作业和转换作业数量，读现有的 `/api/levi/pool/jobs` 和 `/api/levi/jobs`，首次加载、打开菜单、切回标签页时各查一次，标签页可见时每 60 秒一次（有作业在跑时每 5 秒一次；隐藏时不请求，上一个请求未返回时不再发）。菜单在作业上报进度时显示运行中作业的进度，并通向训练池和转换与审核。看到在运行、之后完成、完成但有错误或失败的作业，会用 Toast 提示一次，带通往对应页面的按钮（失败的提示保留到被关闭）；加载时就已结束的、已取消的和被中断的不提示 |
| 命令面板（`command-palette.tsx`、`commands.ts`） | macOS 上 ⌘K，其他系统 Ctrl+K，或点“搜索”。组合框加列表框：跳到页面或数据集（目录里的本地数据集和公开示例，打开面板时从 `/api/levi/catalog` 读取），打开 Agent 工作台、账号与连接或快捷键总表，选择外观或语言。按两种语言的标签以及中英文关键词匹配 |
| 快捷键总表（`shortcuts-dialog.tsx`） | 按 `?` 打开（在输入框中不触发）。列出全局快捷键、跳转组合键和页面已有的快捷键（片段查看器、标注、审核队列） |
| 设置菜单（`levi-header.tsx`） | 账号与连接、命令面板、快捷键，以及动效设置：跟随系统（默认）、减少、正常（见[动效](#动效)） |
| 错误页和“找不到页面”（`error-pages.tsx`；`app/not-found.tsx`、`app/error.tsx`、`app/global-error.tsx`） | 框架自己回答的页面，用全局框架的外观和读者的语言，分三段（发生了什么、为什么、怎么办）：地址不存在时给出回首页和探索数据；没人接住的错误给出重试，并把消息折叠在“技术细节”里。它们正好占满顶栏以下的空间，页面不会出现滚动条；最后兜底的边界会替换整个布局，自带 `<html>`、令牌样式表，并同时写两种语言 |

**快捷键。** 全局框架绑定 ⌘K / Ctrl+K、`?`（也接受全角 `？` 和用 AltGr 打出的 `?`）和跳转组合键：先按 `G`，再按 `H` 首页、`E` 探索数据、`W` 转换与审核、`P` 训练池、`L` 实时评测、`R` 报告、`U` 使用指南（第二个键要在 1.5 秒内；见 `global-keys.ts` 的 `CHORD_PAGES`）。输入法组字时都不触发，`?` 和组合键在输入框中不触发，另一个模态对话框（确认框、页面自己的对话框、原生 `showModal()` 对话框）打开时也都不触发。在模态层里按的键（Tab 和 Esc 除外，由层自己处理）不会传到 `window` 上的监听，页面快捷键不会在对话框背后生效，和原生 `confirm()` 一样；确认框开着时切换页面，按“取消”作答。页面保留自己的快捷键：Space、↑/↓、J/K、Esc、Ctrl/⌘+S/Z/Y。

**加载遮罩。** `loading-component.tsx` 是 `role="status"` 加 `aria-busy="true"`，不是对话框：不拿焦点，也不困住焦点。

**Tailwind** 不扫描 `docs/`（`globals.css` 里的 `@source not "../../docs"`）：Markdown 不是界面代码，其中的词不应生成样式。

## 品牌标识

全站只有一个标识：32 单位网格上的石墨色方块，里面是几何的“L”和一个方点。`src/components/shell/brand.tsx` 里的 `LEVI_MARK` 是它唯一的定义；`<LeviMark size>` 在页面里画它（方块用 `--ds-accent`，字形用 `--ds-on-accent`，所以和主要按钮一样，浅色下黑、深色下白），`<LeviWordmark>` 再加上名称。顶栏画字标；首页、使用指南和报告页画图标。浏览器标签图标 `src/app/icon.svg`（黑色方块，浏览器为深色主题时换成白色）、`apple-icon.png`（180 px，满版）和 `favicon.ico`（16、32、48 px）按同一组数字生成：改了标识后运行 `uv run --with pillow python scripts/brand_icons.py`；`icon.svg` 与定义不一致时 `brand.test.ts` 失败。页面不再自己画标识，酸橙绿已经去掉。

## 全局页面基础（第 5 阶段）

`globals.css` 是唯一的全局样式表。它用令牌设置页面：背景 `--ds-bg`、主文字、正文字号的系统字体、`color-scheme: light dark`（滚动条、下拉列表、日期选择器随主题变化）、低调的滚动条、选中文字底色、给吸顶顶栏留出的 `scroll-padding-top`、等宽数字（`.tabular`）、所有元素统一的石墨色焦点环（`outline: 2px`，外偏 2 px；强制颜色模式下用 `Highlight`）、按钮的指针和禁用外观，以及复选框、单选框、滑块和 `<progress>` 的强调色。中文下仍留的少数全大写、加宽字距标签按正常排版显示（`ds-eyebrow` 也一样：`:lang(zh)`）。它还把 Tailwind 的 `font-sans`、`font-mono` 和 `text-on-media`（视频和图像上的白色）映射到令牌。

**旧名称已经没有了。** `levi.css`（重构前的页面样式）和把旧代码指向令牌的映射都已删除：`--bg`、`--surface-*`、`--text-*`、`--accent*`、`--border-*`，以及被重映射的 Tailwind `white`、`slate`、`cyan`、`lime` 和状态色。Tailwind 色板类又变回 Tailwind 自己的颜色，所以代码里不能再留下它们（有测试查）。从旧分支移植代码时：

| 旧名称 | 改用 |
| --- | --- |
| `--bg`、`--surface-0/1/2` | `--ds-bg`、`--ds-surface-sunken`、`--ds-surface-1`、`--ds-surface-2` |
| `--text-primary/muted/faint` | `--ds-text-primary/secondary/tertiary` |
| `--accent`、`--accent-soft`、`--accent-ring` | `--ds-accent` 只用于主要按钮、选中和进度；选中的底色用 `--ds-surface-selected`；`--ds-focus-ring` |
| `--border-subtle`、`--border-strong` | `--ds-separator`、`--ds-separator-strong`；输入框用 `--ds-border-control` |
| Tailwind `white`（`text-white`、`border-white/10`、`bg-white/5`） | `--ds-text-primary`、`--ds-separator`、`--ds-surface-hover`；视频上的文字用 `text-on-media` |
| Tailwind `slate-*`、`zinc-*` | 文字令牌（主、次要、三级）、`--ds-separator(-strong)`、`--ds-surface-1`、`--ds-bg` |
| Tailwind `cyan-*`、`lime-*` | `--ds-accent`；`Button variant="primary"`；`--ds-surface-selected` |
| Tailwind `red-*`、`orange/amber/yellow-*`、`green/emerald-*`、`blue-*` | 经 `Badge` 或 `StatusDot` 用 `--ds-danger`、`--ds-warning`、`--ds-success`、`--ds-info`；图表序列用 `--ds-data-*` |
| `.levi-box`、`.levi-eyebrow`、`.levi-primary`、`.levi-secondary`、`.levi-input`、`.levi-table`、`.levi-status`、`.levi-error`、`.panel` | `Card` / `EmptyState`、`ds-eyebrow`、`Button`、`Field` 里的 `Input` / `Select` / `Textarea`、`Table`、`Badge`、三段式错误（`Problem`）、`Card` |

`global-styles.test.ts` 在 `globals.css`、`home.css`、`reading.css`、`report.css` 出现颜色字面量、出现旧配色、旧变量名或旧映射、`levi.css` 重新出现，或 `src/` 任何地方出现 Tailwind 色板类时失败。

**首页**（`/`）是工作入口：继续（这个浏览器里最近打开的片段或数据集；框架把访问记录存在 `localStorage` 的 `levi-recent` 里，不发送到任何地方）、需要你处理（`waiting_for` 为人工批准、审核或提交的 agent 任务，来自 `/api/levi/agent/v1/activity/tasks`）、正在运行（转换和训练池作业及其进度）、显示实时评测时的状态卡，以及最近的数据集（先列打开过的，再列其他已登记的）。每张卡单独加载，先显示骨架，10 秒没有回应就放弃；标签页可见时每 15 秒刷新。Hugging Face 搜索和旧的 `/?path=`、`/?dataset=` 链接保留。原来的介绍移到了使用指南。

**阅读型版式**（`reading.css`，使用指南和报告页）：单栏，最宽 760 px；左侧目录始终可见并标出当前小节（`aria-current="location"`）；正文 16/26（`--ds-text-reading-*`）；`.levi-prose` 统一 Markdown 的标题、列表、链接（主文字加下划线）、引用、代码和表格。目录在小节标题越过窗口 40% 高度处的一条线后才标出该小节（`active-section.ts`），标记不会比内容慢一节；中文里强调用加粗一档的字重，不用计算出来的斜体；Markdown 表格的第一列至少保留 6.5 em 宽。使用指南的示例缩略图在帧加载出来之前（远程文件连不上时一直如此）显示安静底色上的图标，不再是一块黑。报告页的图表使用 `--ds-data-*`（经 `useCssTokens` 取值；当前浏览器里 Recharts 也能直接用 `var(--ds-…)`，这个 hook 只在 canvas、WebGL 里必需），运行中状态只用颜色和文字表示、没有循环动画，“已更新”用全局 Toast 提示。
## 片段查看器（第 3 阶段）

片段查看器（`src/app/[org]/[dataset]/[episode]/`）在浅深两套主题下都用令牌。样式在 `src/components/viewer/`：

| 内容 | 位置 |
| --- | --- |
| 框架、标签栏、片段列表、媒体、播放、提示、指标卡 | `viewer.css`（类名 `vw-*`） |
| 标注面板、时间轴、价值模型和锚定复核泳道、分割 | `annotations.css`（作用域 `.annotations-skin`，取代 `annotations-skin.css`） |
| 数据配色 | `viewer.css`（`--dv-1` … `--dv-8` 即令牌 `--ds-data-1` … `--ds-data-8`、`--dv-positive`、`--dv-negative`、`--dv-neutral`）和 `data-palette.ts` |
| 标签页与“分析”标签 | `viewer-tabs.ts`、`analysis-tab.tsx` |
| 错误页 | `load-error.tsx` |
| 测试 | `src/components/viewer/__tests__/` |

**标签页**：片段、标注、三维回放（机器人受支持时）、统计、帧概览、**分析**。“分析”用分段控件收纳原来的动作洞察、筛选、数据诊断三个标签，每个视图加载的数据和原标签完全一样。旧会话存下的标签 id（`insights`、`filtering`、`doctor`）会打开“分析”并选中对应视图；视图存在 `sessionStorage`（`analysisView`）。标签栏和标注子标签都是 ds `Tabs`（←/→ 切换）。

**检查器**：“标注”标签右侧有一栏（320 px，`inspector.tsx`），显示选中时间片段的编辑表单；在“物体标注”下显示选中物体的信息和接受/拒绝。表单仍由各面板渲染，状态和处理函数不变，`InspectorPortal` 只把表单的 DOM 移到这一栏；没有这一栏时（`useInspectorSlot()` 为 null）表单留在原处。这一栏可以收起成窄条；窗口窄于 1200 px 时变成底部抽屉，默认收起。Esc、Ctrl/⌘+S/Z/Y 照常可用（它们监听的是 window）。

**数据色**（时间片段、图表序列、掩码、视频上的标签）使用[数据色](#数据色)一节描述的配色，从不用强调色。文字从不使用数据色：标签胶囊、泳道名、图例都是文字色，旁边配一个彩色圆点或色条。

**媒体区**两种主题下都是黑底（`--ds-media-bg`）；每个相机画面和三维视口带 `data-theme="dark"`，画在上面的控件是深色的。视频上的标签是深色底板上的近白文字加一条彩色竖条。三维背景是黑色。

**片段列表**：结局用形状加文字表示（对勾圆、叉圆、空心圆；人工标注的外加一圈），仍是按钮，点击依次切换标签；标记是可按下的切换按钮；“失败”“已标记”是筛选胶囊。标题行有上一个/下一个片段按钮，作用与 ↑/↓ 相同。

**保留的快捷键**：空格（播放/暂停）、↑/↓（换片段）、标注编辑里的 Esc 和 Ctrl/⌘+S/Z/Y，与原来一致；输入时都不触发。

**反馈**：加载遮罩 300 ms 后才出现（加载快就不显示），减少动态效果时转圈停止；其他地方的转圈都换成 Lucide 的转圈；页面错误说明发生了什么、原因（技术细节）和怎么办（重试、返回探索数据）；“数据集已变化”卡片放在左下角，不挡住 Toast。

ESLint 的颜色规则和 `src` 里所有文件一样覆盖查看器的文件；例外只有 `data-palette.ts`（给 canvas 和 WebGL 用的数值配色）和 URDF 查看器（机器人模型的材质和灯光颜色）。
## 页面（第 4 阶段）

实时评测（`/live`）、转换与审核（`/workbench`）、训练池（`/pool`）、探索数据（`/explore`）和 Agent 工作台抽屉里的内容都已改用令牌，随主题切换。接口调用、作业和数据都没变，只改了标记、类名和反馈方式。

| 内容 | 位置 |
| --- | --- |
| 页面样式（类名 `pg-*`） | `src/components/pages-ui/pages.css`，由四个页面引入 |
| Agent 工作台内容样式 | `src/components/pages-ui/agent-content.css`（规则都在 `.levi-agent-sheet` 之下；抽屉本身属于全局框架） |
| 反馈组件 | `src/components/pages-ui/feedback.tsx` |
| 测试 | `src/components/pages-ui/__tests__/`、`src/components/pool/__tests__/composition-order.test.tsx` |

- **类名**：这些页面原来用 `levi.css` 里的 `levi-*` 类，现在改写为 `pg-*`，布局不变，颜色换成令牌；按钮、输入框和表格直接用 `ds-btn`、`ds-input`、`ds-table`（或对应组件）。Agent 工作台内容保留 `levi-agent-*`、`levi-activity-*`、`levi-connection-*` 类名（与抽屉共用），`agent-content.css` 在 `.levi-agent-sheet` 下用令牌重写；其中的按钮和输入框加了 `ds-btn` / `ds-input`，按下的按钮（标签、开关）显示为选中态。两份样式表都没有颜色字面量（有测试），这些页面的 TSX 也和 `src` 里所有文件一样受颜色 lint 规则约束。`levi.css` 删除之后，这些规则自成一体（背后没有更旧的规则）；其中提到它的注释属于历史说明。
- **反馈组件**（以后可提升进 `ds`）：`Problem` 是三段式错误（发生了什么、为什么——通常是服务端原话——、怎么办，可选折叠的“技术细节”；默认 `role="alert"`，常驻的错误用 `live={false}`）；`RequestProblem` 写明哪个操作失败，并把服务端消息作为原因；`Note` 是行内的信息/成功/警告提示；`JobCard` 是各页统一的作业卡（状态徽章、标题、右侧元数据，下面放该页的进度和结果）；`EmptyLine` 是卡片内的一行空状态。
- **状态**一律用 `Badge` 或 `StatusDot`（形状、颜色、文字）：训练池作业状态、转换的检查项、实时评测的会话和服务状态（运行中的会话会呼吸）、片段结局。进度条用 `Progress`。
- **加载和结果**：布局已知的地方用骨架屏（训练池预览、选中片段、实时评测统计）；结果不在操作旁边时（配方已保存、记录已清除、已释放空间）用 Toast；错误留在出错的操作旁边。
- **训练池的任务顺序**改用 `ReorderList`（Motion）：拖动手柄，或聚焦手柄后按 ↑/↓；每个任务仍保留上移、下移和移除按钮。
- **每屏一个主要按钮**：登记并浏览（转换与审核）、开始导出（训练池；从未扫描时是“立即扫描”）、有计划后的“运行转换”。
- **页面版式**：训练池加了步骤条（① 选择 ② 组合 ③ 导出，`pool-steps.tsx`；当前步骤随组合和导出变化，点击跳到对应区块），标题行放“导出…”作为本页主要操作（滚到导出表单并聚焦名称；只有从未扫描时“立即扫描”是主要按钮）；“最近的作业”默认折叠，并提示顶栏“作业”菜单。实时评测的空状态把反引号里的命令画成 `<code>`，并用一个按钮复制第一条命令（`command-text.tsx`）；统计里的选择框有描边；面板不再变淡：服务没运行时用文字说明，因为降低不透明度会让文字低于 4.5:1。实时评测顶部加一行汇总（运行中的会话、已标注/已完成片段、最近一次错误；`live-summary.tsx`），“只看不控”的说明改为信息色 `Note`。转换与审核里，主要按钮归向导的当前步骤（检查输入、审核计划、运行转换，完成后“审核转换结果”；转换运行中没有主要按钮）；选中的导出卡片显示为按下并描边。
- **Agent 工作台内容**：分区改用 ds `Tabs`；运行操作里“运行试点”或“执行剩余”是主要按钮；批准计划、接受试点、提交更改带 `HumanActionMark`（“需要你确认”），并作为该步的主要按钮。
- **共享组件** `hf-auth-button.tsx`（ds 按钮、菜单、对话框）和 `dataset-format.tsx`（ds 徽章）自带样式（`pages-ui/shared.css`），在所有页面（包括片段查看器）外观一致。
- **辅助类**：`pg-small`、`pg-mt-2…6`、`pg-my-2/3`、`pg-full`、`pg-block`、`pg-mono`、`pg-between`、`pg-stack` 取代这些页面上的 Tailwind 间距和字号工具类：`ds-root` 在层外重置了标题和段落边距，层内的工具类在那里不生效（有测试防止回退）。`RequestProblem` 去掉“Error:”前缀（`cleanMessage`），支持 `onRetry`（“重试”按钮）。
- **减少动态效果**：`pages.css` 和 `agent-content.css` 里所有有动画或过渡的规则，在 `data-motion="reduce"`（应用内开关）下也会停止，有测试。
- **数据色**：对象工具的掩码叠加从画布元素的 CSS 颜色读取 `--ds-data-6`（canvas 读不到 CSS 变量）。
- **已知缺口**：被截断的表格单元格仍用原生 `title` 显示全文（符合 `title=` 规则：单元格已经显示了关键内容）；两个会话报告同一模型和任务目录时，实时评测会话列表可能出现 React 重复 key 警告（取决于数据，这里没改）。

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

**应用内动效设置**（设置菜单）：*跟随系统*（默认）、*减少*或*正常*，存在 `levi-motion` 键下，由 `<head>` 里的脚本在首次绘制前写到 `<html>` 上，成为 `data-motion="reduce"` 或 `data-motion="full"`（跟随系统时不写），与主题的处理方式相同。*减少*的效果和系统设置相同（上面的令牌，以及所有读 `data-motion="reduce"` 的规则）；*正常*则在系统要求减少动态效果时仍保留动效令牌（媒体查询里的 `:root:not([data-motion="full"])`）。渲染内容会变的组件（`usePrefersReducedMotion`、`ReorderList`）实时跟随它。页面样式表里直接写在 `@media (prefers-reduced-motion)` 下的规则，在*正常*下仍只跟随系统；它们各自也有 `data-motion="reduce"` 的对应规则，用于*减少*。

禁止无限循环的装饰动画、视差、滚动劫持、超过 400 ms 的动画和阻塞操作的动画。

**Motion**（`motion` 包，即原 Framer Motion，MIT 许可）只用在 `ReorderList` 里，gzip 后约 45 KB，只有引入该组件的页面才会加载。

## 可访问性

- 焦点环：2 px `--ds-focus-ring`，外偏 2 px（每个可交互组件都带 `ds-focus` 类）；Windows 高对比模式下用 `Highlight`。
- 每个弹出层都有焦点陷阱（监听挂在 `document` 上，焦点跑到哪里都管得住），Esc 关闭，关闭后焦点返回；菜单和标签页遵循 ARIA 模式；Toast 用一个 polite 和一个 assertive 的 live 区域。
- 可点区域至少 24 × 24 px。
- 被禁用的控件在旁边用文字说明原因，并用 `aria-describedby` 关联（实时评测片段列表的移除功能是范例）；吸顶的顶栏不会挡住获得焦点的元素或锚点（`scroll-padding-top`、`scroll-margin-top`）。

## 样张页

`/design` 并排展示浅色和深色下的全部令牌和组件（`?only=light` 或 `?only=dark` 只显示一套，`?motion=reduce` 以减少动态效果开始）。`next dev` 下可访问；生产服务（`next start`、`levi serve`）里，除非环境里设了 `LEVI_DESIGN_PAGE=1`（`src/lib/design/gate.ts`），`src/middleware.ts` 会在路由之前直接返回纯文本 404，不发送页面的元数据和样式。导航里没有它的入口。

## 迁移状态

| 范围 | 状态 |
| --- | --- |
| 全局框架：顶栏、Toast、确认框、命令面板、快捷键总表、Agent 工作台抽屉 | 已用 `ds-*` 组件和令牌（`shell.css`） |
| 首页、使用指南、报告页 | 令牌、`ds-*` 组件、Lucide 图标；首页和使用指南共用 `home.css` 和 `reading.css` |
| 实时评测、转换与审核、训练池、探索数据 | `pg-*` 样式配令牌，使用 ds 按钮、输入框、表格、徽章和反馈组件（`pages-ui/`） |
| Agent 工作台内容 | 保留 `levi-agent-*`、`levi-activity-*`、`levi-connection-*` 类名（抽屉共用）；`agent-content.css` 在 `.levi-agent-sheet` 下给它们设样式；其中的控件带 `ds-btn` / `ds-input` |
| 片段查看器 | 框架、标签、片段列表、播放和检查器用 `vw-*` 样式和 ds 组件（`viewer/`）；标注面板、时间轴和泳道用 `annotations.css`；分析视图（动作洞察、筛选、统计、概览、数据诊断）和三维查看器用 Tailwind 工具类自己排版，颜色是令牌值（`text-(--ds-text-secondary)`），还不是 `ds-*` 组件 |
| 颜色 | CSS 和 TSX 里只用令牌，检查覆盖整个 `src/`（两个有记录的例外）；`levi.css`、旧变量和 Tailwind 重映射已删除 |
| 图标 | 经 `Icon` 用 Lucide；剩下的文字箭头和对勾出现在正文、键盘提示和三维查看器的 HUD 里 |
| 找不到页面和错误页 | 全局框架的外观，中英双语（`error-pages.tsx`）；框架默认页已去掉 |
| 语言 | 每个 `t("…")` 字面量在两份目录里都有键（有测试）；两份目录的键相同 |

已知并有意保留的缺口：实时评测会话列表里取决于数据的 React 重复 key 警告（见“页面”），以及被截断的表格单元格上的原生 `title`（见 `title=` 规则）。

## 测试

`global-styles.test.ts` 检查页面基础、吸顶顶栏、字标、`levi.css` 的缺席、旧名称和 Tailwind 色板类；`brand.test.ts` 检查标识；`route-title.test.ts` 检查标签页标题；`lint-config.test.ts` 和 `src/__tests__/eslint-hex.test.ts` 检查颜色规则（覆盖每个源文件，例外都列出并带注释，`rgb()` / `hsl()` 也会被拦住）；`src/i18n/__tests__/catalog.test.ts` 检查语言目录；`recent.test.ts` 和 `home-data.test.ts` 检查首页的数据；`report-blocks.test.tsx` 检查报告页的排序表头（`aria-sort`、图标、提示）、指标变化图标和状态图标；`shell-dom.test.tsx` 检查组合键、命令面板里的数据集和对话框；`jobs-toast.test.tsx` 检查作业结果和进度；`motion-preference.test.tsx` 检查动效设置；`error-pages.test.tsx` 检查“找不到页面”和错误页。

`bun test` 在 DOM（happy-dom，开发依赖）里运行组件测试。组件测试先引入 `./dom` 并调用 `setupDom()`，其中有 `render`、`press`、`click`、`focus`、`dropFocus`、`fire`、`flush`、`mockMatchMedia`。每个这样的测试文件结束后会移除 DOM 全局对象，其他测试仍在没有 DOM 的环境里运行。`tokens-contrast.test.ts` 还会在组件或样张页的 TSX 里发现颜色字面量（十六进制、`rgb()`、`hsl()`）或 Tailwind 任意值时报错。
