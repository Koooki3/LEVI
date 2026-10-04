"use client";
/**
 * Design specimen: every token and component of the design system, in light
 * and dark side by side. Development page (see page.tsx); its sample text is
 * deliberately bilingual and not in the locale catalogs.
 */
import dynamic from "next/dynamic";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Archive,
  ArrowRight,
  Bot,
  Camera,
  Check,
  ChevronRight,
  Copy,
  Database,
  Download,
  FolderOpen,
  Layers,
  ListFilter,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings,
  SkipBack,
  SkipForward,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Divider,
  EmptyState,
  Field,
  Icon,
  IconButton,
  Input,
  Kbd,
  Menu,
  Progress,
  Radio,
  RadioGroup,
  SegmentedControl,
  Select,
  Sheet,
  Skeleton,
  SkeletonText,
  Spinner,
  StatusDot,
  Switch,
  Table,
  TableRow,
  Tabs,
  Tag,
  Textarea,
  ThemePicker,
  ToastProvider,
  Tooltip,
  useToast,
} from "@/components/ds";
import type { ReorderList as ReorderListType } from "@/components/ds/ReorderList";
import { applyTheme, useThemePreference } from "@/lib/design/theme";

// Motion loads only with this list (next/dynamic drops the generic type).
const ReorderList = dynamic(
  () => import("@/components/ds/ReorderList").then((m) => m.ReorderList),
  { ssr: false, loading: () => <SkeletonText lines={4} /> },
) as typeof ReorderListType;

type Theme = "light" | "dark";

const LIGHT_STEPS = Array.from({ length: 12 }, (_, i) => `--ds-gray-l-${i}`);
const DARK_STEPS = Array.from({ length: 13 }, (_, i) => `--ds-gray-d-${i}`);

function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="dsp-section">
      <h2 className="ds-text-title-2">{title}</h2>
      {note && <p className="ds-text-callout ds-text-secondary">{note}</p>}
      <div className="dsp-section__body">{children}</div>
    </section>
  );
}

function Swatches({ theme }: { theme: Theme }) {
  const ref = useRef<HTMLDivElement>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const steps = theme === "light" ? LIGHT_STEPS : DARK_STEPS;
  useEffect(() => {
    if (!ref.current) return;
    const style = getComputedStyle(ref.current);
    setValues(
      Object.fromEntries(
        steps.map((name) => [name, style.getPropertyValue(name).trim()]),
      ),
    );
  }, [steps]);
  return (
    <div ref={ref} className="dsp-swatches">
      {steps.map((name, index) => (
        <div key={name} className="dsp-swatch">
          <span
            className="dsp-swatch__chip"
            style={{ background: `var(${name})` }}
          />
          <span className="ds-text-caption">gray-{index}</span>
          <span className="ds-text-caption ds-text-secondary ds-mono">
            {values[name] ?? ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function ToastDemo() {
  const toast = useToast();
  const shown = useRef(false);
  useEffect(() => {
    if (shown.current) return;
    shown.current = true;
    toast.show({
      tone: "success",
      title: "Episode 14 saved",
      description: "片段 14 已保存。",
      duration: null,
      action: { label: "View", onClick: () => undefined },
    });
  }, [toast]);
  return (
    <div className="dsp-row">
      <Button
        onClick={() =>
          toast.show({ tone: "success", title: "Training pool exported" })
        }
      >
        Success toast
      </Button>
      <Button
        onClick={() =>
          toast.show({
            tone: "danger",
            title: "Export failed",
            description: "The disk is full. Free 12 GB and retry.",
            action: { label: "Retry", onClick: () => undefined },
          })
        }
      >
        Error toast (stays)
      </Button>
      <Button
        variant="ghost"
        onClick={() =>
          toast.show({
            tone: "info",
            title: "3 episodes removed",
            action: { label: "Undo", onClick: () => undefined },
          })
        }
      >
        Undo toast
      </Button>
    </div>
  );
}

function Panel({
  theme,
  reduceMotion,
}: {
  theme: Theme;
  reduceMotion: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState("episode");
  const [segment, setSegment] = useState("timeline");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [switchOn, setSwitchOn] = useState(true);
  const [replay, setReplay] = useState(0);
  const [selectedRow, setSelectedRow] = useState(1);
  const [order, setOrder] = useState([
    { id: "plates", label: "plates · 100 episodes" },
    { id: "screws", label: "screws · 60 episodes" },
    { id: "eggplant", label: "eggplant · 91 episodes" },
    { id: "cups", label: "cups · 24 episodes" },
  ]);
  const [tags, setTags] = useState(["grasp", "transport", "place"]);

  return (
    <div
      ref={panel}
      className="ds-root dsp-panel"
      data-theme={theme}
      data-motion={reduceMotion ? "reduce" : undefined}
    >
      <ToastProvider contained>
        <header className="dsp-panel__header ds-material">
          <strong className="ds-text-title-3">
            {theme === "light" ? "Light 浅色" : "Dark 深色"}
          </strong>
          <span className="ds-text-caption ds-text-secondary">
            accent A · graphite
          </span>
        </header>

        <Section
          title="Gray scale"
          note="Raw steps; components use semantic tokens."
        >
          <Swatches theme={theme} />
        </Section>

        <Section title="Type">
          <div className="dsp-stack">
            <p className="ds-text-display">Every motion, clearly</p>
            <p className="ds-text-title-1">Title 1 · 页面标题 24/32</p>
            <p className="ds-text-title-2">Title 2 · 区块标题 19/26</p>
            <p className="ds-text-title-3">Title 3 · 卡片标题 16/22</p>
            <p className="ds-text-body">
              Body 14/21 — review the time segments of episode 14.
              正文：审核片段 14 的时间片段。
            </p>
            <p className="ds-text-callout ds-text-secondary">
              Callout 13/19, secondary text · 次要文字
            </p>
            <p className="ds-text-caption ds-text-tertiary">
              Caption 12/16, tertiary · 三级文字 · 2026-10-04 14:32
            </p>
            <p className="ds-text-body ds-mono ds-num">0123456789 · 12.40 s</p>
          </div>
        </Section>

        <Section
          title="Surfaces"
          note="Background, card, sunken, raised, glass."
        >
          <div className="dsp-surfaces">
            <div className="dsp-surface ds-surface-sunken ds-on-sunken">
              <span>Sunken</span>
              <span className="ds-text-caption ds-text-tertiary">
                tertiary → secondary
              </span>
            </div>
            <div className="dsp-surface ds-surface-1">
              <span>Card</span>
              <span className="ds-text-caption ds-text-tertiary">
                surface-1
              </span>
            </div>
            <div className="dsp-surface ds-surface-2 ds-on-raised">
              <span>Raised</span>
              <span className="ds-text-caption ds-text-tertiary">
                surface-2
              </span>
            </div>
          </div>
          <div className="dsp-media ds-media">
            <div className="dsp-media__frame" aria-hidden="true" />
            <div className="dsp-media__bar ds-material">
              <IconButton icon={SkipBack} label="Previous frame" size="sm" />
              <IconButton
                icon={Play}
                label="Play"
                size="sm"
                shortcut={<Kbd>Space</Kbd>}
              />
              <IconButton icon={SkipForward} label="Next frame" size="sm" />
              <span className="ds-text-caption ds-num">
                00:02.40 / 00:12.00
              </span>
            </div>
          </div>
          <p className="ds-text-caption ds-text-secondary">
            Media areas stay black in both themes; the glass bar floats over
            them.
          </p>
        </Section>

        <Section title="Buttons">
          <div className="dsp-row">
            <Button variant="primary" icon={Check}>
              Approve
            </Button>
            <Button>Secondary</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="danger" icon={Trash2}>
              Delete
            </Button>
          </div>
          <div className="dsp-row">
            <Button variant="primary" size="sm">
              Small
            </Button>
            <Button variant="primary">Medium</Button>
            <Button variant="primary" size="lg" iconEnd={ArrowRight}>
              Large
            </Button>
            <Button variant="primary" loading>
              Saving
            </Button>
            <Button disabled>Disabled</Button>
          </div>
          <Divider />
          <div className="dsp-row">
            <IconButton icon={Pencil} label="Edit" />
            <IconButton icon={Copy} label="Copy" shortcut={<Kbd>⌘C</Kbd>} />
            <IconButton icon={Settings} label="Settings" variant="secondary" />
            <IconButton icon={ListFilter} label="Filter" pressed />
            <span className="dsp-focus-sample ds-btn ds-btn--secondary ds-btn--md">
              Focus ring
            </span>
          </div>
        </Section>

        <Section
          title="Selection and table"
          note="Selected row: grey fill, weight 500, 2 px bar."
        >
          <Table caption="Episodes">
            <thead>
              <tr>
                <th>Episode</th>
                <th>Task</th>
                <th>Outcome</th>
                <th className="ds-num">Length</th>
              </tr>
            </thead>
            <tbody>
              {[
                [12, "stack plates", "success", "11.2 s"],
                [13, "stack plates", "failure", "14.8 s"],
                [14, "insert screws", "unlabeled", "9.6 s"],
              ].map(([id, task, outcome, length], index) => (
                <TableRow
                  key={id}
                  selected={index === selectedRow}
                  onClick={() => setSelectedRow(index)}
                >
                  <td className="ds-num">{id}</td>
                  <td>{task}</td>
                  <td>
                    <StatusDot
                      tone={
                        outcome === "success"
                          ? "success"
                          : outcome === "failure"
                            ? "danger"
                            : "neutral"
                      }
                    >
                      {outcome}
                    </StatusDot>
                  </td>
                  <td className="ds-num ds-text-tertiary">{length}</td>
                </TableRow>
              ))}
            </tbody>
          </Table>
        </Section>

        <Section
          title="Status"
          note="Shape + colour + words; never colour alone."
        >
          <div className="dsp-row">
            <Badge tone="success">Done</Badge>
            <Badge tone="warning">Waiting for review</Badge>
            <Badge tone="danger">Failed</Badge>
            <Badge tone="info">Suggested</Badge>
            <Badge>Planned</Badge>
          </div>
          <div className="dsp-row">
            <StatusDot tone="success" live>
              Running · 3 sessions
            </StatusDot>
            <StatusDot tone="warning">Paused</StatusDot>
            <StatusDot tone="danger">Fault</StatusDot>
          </div>
          <div className="dsp-row">
            {tags.map((tag) => (
              <Tag
                key={tag}
                onRemove={() =>
                  setTags((list) => list.filter((t) => t !== tag))
                }
              >
                {tag}
              </Tag>
            ))}
            <Tag>read-only</Tag>
          </div>
        </Section>

        <Section title="Form controls">
          <div className="dsp-form">
            <Field
              label="Dataset name"
              hint="Lowercase letters, digits and dashes."
            >
              <Input placeholder="screws-dev" defaultValue="screws-frozen" />
            </Field>
            <Field
              label="Export folder"
              error="This folder already holds an export."
              required
            >
              <Input defaultValue="/exports/plates" />
            </Field>
            <Field label="Task instruction">
              <Textarea defaultValue="Put the two screws in the box." />
            </Field>
            <Field label="Reading mode">
              <Select defaultValue="anchored">
                <option value="sampled">Evenly sampled frames</option>
                <option value="anchored">Release-anchored review</option>
              </Select>
            </Field>
            <div className="dsp-stack">
              <Checkbox label="Include failures" defaultChecked />
              <Checkbox
                label="Cameras"
                description="2 of 3 selected"
                indeterminate
              />
              <Checkbox label="Held-out episodes" disabled />
            </div>
            <RadioGroup legend="Outcome">
              <Radio name={`outcome-${theme}`} label="Success" defaultChecked />
              <Radio name={`outcome-${theme}`} label="Failure" />
            </RadioGroup>
            <Switch
              label="Live overlay"
              description="Draw masks while the episode plays."
              checked={switchOn}
              onChange={(event) => setSwitchOn(event.target.checked)}
            />
          </div>
        </Section>

        <Section title="Navigation">
          <Tabs
            label="Episode viewer"
            value={tab}
            onChange={setTab}
            items={[
              {
                id: "episode",
                label: "Episode",
                content: <p>Videos and signals.</p>,
              },
              {
                id: "annotate",
                label: "Annotate",
                content: <p>Time segments.</p>,
              },
              {
                id: "analysis",
                label: "Analysis",
                content: <p>Insights, filters, diagnostics.</p>,
              },
              { id: "replay", label: "3D replay", disabled: true },
            ]}
          />
          <div className="dsp-row">
            <SegmentedControl
              label="View"
              value={segment}
              onChange={setSegment}
              options={[
                { value: "timeline", label: "Timeline" },
                { value: "frames", label: "Frames" },
                { value: "charts", label: "Charts" },
              ]}
            />
            <ThemePickerDemo />
          </div>
          <div className="dsp-row">
            <Menu
              label="Actions"
              items={[
                {
                  id: "open",
                  label: "Open dataset",
                  icon: FolderOpen,
                  shortcut: <Kbd>⌘O</Kbd>,
                  onSelect: () => undefined,
                },
                {
                  id: "export",
                  label: "Export…",
                  icon: Download,
                  onSelect: () => undefined,
                },
                {
                  id: "archive",
                  label: "Archive",
                  icon: Archive,
                  disabled: true,
                  onSelect: () => undefined,
                },
                {
                  id: "delete",
                  label: "Delete",
                  icon: Trash2,
                  tone: "danger",
                  onSelect: () => setConfirmOpen(true),
                },
              ]}
            />
            <span className="ds-text-callout ds-text-secondary">
              Command palette <Kbd>⌘</Kbd>
              <Kbd>K</Kbd>
            </span>
          </div>
        </Section>

        <Section
          title="Dialogs and sheets"
          note="Static preview; the buttons open the real ones (focus trap, Esc, focus returns)."
        >
          <div className="ds-dialog ds-dialog--sm ds-on-raised dsp-static-dialog">
            <header className="ds-dialog__header">
              <h3 className="ds-dialog__title">Delete model seg-v3?</h3>
            </header>
            <p className="ds-dialog__description">
              Its labelling jobs stay; the model file is removed and cannot be
              restored.
            </p>
            <footer className="ds-dialog__footer">
              <Button>Cancel</Button>
              <Button variant="danger">Delete</Button>
            </footer>
          </div>
          <div className="dsp-row">
            <Button variant="danger" onClick={() => setConfirmOpen(true)}>
              Open confirm dialog
            </Button>
            <Button onClick={() => setSheetOpen(true)}>Open sheet</Button>
          </div>
          <ConfirmDialog
            open={confirmOpen}
            tone="danger"
            title="Delete model seg-v3?"
            description="The model file is removed and cannot be restored."
            confirmLabel="Delete"
            onConfirm={() => setConfirmOpen(false)}
            onCancel={() => setConfirmOpen(false)}
          />
          <Sheet
            open={sheetOpen}
            onClose={() => setSheetOpen(false)}
            title="Agent workbench"
            description="A drawer from the right instead of a floating window."
            footer={
              <Button variant="primary" onClick={() => setSheetOpen(false)}>
                Done
              </Button>
            }
          >
            <p>Sheet body.</p>
          </Sheet>
        </Section>

        <Section title="Feedback">
          <ToastDemo />
          <div className="dsp-grid-2">
            <Card
              padding="compact"
              title="Export plates"
              description="Stage 2 of 3 · about 4 min left"
            >
              <Progress label="Converting episodes" value={62} showValue />
            </Card>
            <Card padding="compact" title="Scanning sources">
              <Progress label="Reading metadata" value={null} />
              <div className="dsp-row">
                <Spinner showLabel label="Loading episodes" />
              </div>
            </Card>
          </div>
          <Card padding="compact" title="Loading" aria-busy="true">
            <div className="dsp-skeleton">
              <Skeleton width={96} height={64} radius="md" />
              <SkeletonText lines={3} />
            </div>
          </Card>
          <Card padding="compact">
            <EmptyState
              icon={Database}
              title="No local datasets yet"
              description="Register a LeRobot dataset or a raw capture folder to start."
              action={
                <Button variant="primary" icon={Plus}>
                  Register dataset
                </Button>
              }
              secondaryAction={<Button variant="ghost">Read the guide</Button>}
            />
          </Card>
        </Section>

        <Section
          title="Icons"
          note="Lucide, 16 / 20 / 24 px, stroke 1.75 / 1.5."
        >
          <div className="dsp-row dsp-icons">
            {[
              Search,
              Play,
              Pause,
              Camera,
              Bot,
              Layers,
              Upload,
              Download,
              RefreshCw,
              ChevronRight,
              X,
              Check,
            ].map((glyph, index) => (
              <Icon key={index} icon={glyph} />
            ))}
          </div>
          <div className="dsp-row dsp-icons">
            <Icon icon={Camera} size="sm" />
            <Icon icon={Camera} size="md" />
            <Icon icon={Camera} size="lg" />
            <Tooltip content="Tooltips open on hover and on keyboard focus">
              <button
                type="button"
                className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
              >
                Hover or Tab here
              </button>
            </Tooltip>
          </div>
        </Section>

        <Section
          title="Motion"
          note={
            reduceMotion
              ? "Reduced motion: no movement, items snap."
              : "Press, pop-in, drag (spring), reorder."
          }
        >
          <div className="dsp-row">
            <Button onClick={() => setReplay((n) => n + 1)} icon={RefreshCw}>
              Replay entrance
            </Button>
          </div>
          <div key={replay} className="dsp-pop">
            <Card padding="compact" title="Menu / popover entrance">
              <p className="ds-text-callout ds-text-secondary">
                200 ms fade + 4 px rise; fade only when motion is reduced.
              </p>
            </Card>
          </div>
          <ReorderList
            label="Recipe sources"
            items={order}
            onReorder={setOrder}
            getKey={(item) => item.id}
            getLabel={(item) => item.label}
            renderItem={(item) => <span>{item.label}</span>}
          />
        </Section>
      </ToastProvider>
    </div>
  );
}

function ThemePickerDemo() {
  const { preference, setPreference } = useThemePreference();
  return <ThemePicker value={preference} onChange={setPreference} />;
}

export default function Specimen({
  only,
  reduceMotion: initialReduce,
}: {
  only: Theme | null;
  reduceMotion: boolean;
}) {
  const [reduceMotion, setReduceMotion] = useState(initialReduce);
  const { preference, resolved } = useThemePreference();
  const top = useRef<HTMLDivElement>(null);
  useEffect(
    () => applyTheme(top.current, only ?? preference),
    [only, preference],
  );
  const themes: Theme[] = only ? [only] : ["light", "dark"];
  return (
    <div ref={top} className="ds-root dsp-page">
      <div className="dsp-intro">
        <p className="ds-eyebrow">LEVI design system · stage 0 / 1</p>
        <h1 className="ds-text-title-1">Design specimen</h1>
        <p className="ds-text-body ds-text-secondary">
          Tokens and components in light and dark. The theme picker stores your
          preference (now: {preference} → {resolved}); this page applies it only
          to its own frame. 样张页：浅色与深色并排。
        </p>
        <Switch
          label="Preview reduced motion"
          checked={reduceMotion}
          onChange={(event) => setReduceMotion(event.target.checked)}
        />
      </div>
      <div className={only ? "dsp-columns dsp-columns--one" : "dsp-columns"}>
        {themes.map((theme) => (
          <Panel key={theme} theme={theme} reduceMotion={reduceMotion} />
        ))}
      </div>
    </div>
  );
}
