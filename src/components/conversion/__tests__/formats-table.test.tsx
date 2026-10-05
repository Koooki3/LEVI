import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";
import { FormatsTable } from "../formats-table";

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

// GET /api/levi/convert/formats (levi/conversion/registry.py capabilities()),
// the fields the table shows.
const FORMATS = {
  inputs: [
    {
      id: "robot_capture",
      label: "Robot capture (pose/gripper CSV + video demos)",
      description:
        "task/demo_NNNN folders with end_effector_pose.csv, gripper_state.csv and one video per camera — from teleoperation or policy rollouts.",
      evidence: "real-data",
    },
    {
      id: "image_sequence",
      label: "Robot capture (pose/gripper CSV + image folders)",
      description:
        "task/demo_NNNN folders with the capture CSVs and one folder of numbered PNG/JPEG frames per camera (rate taken from source_fps).",
      evidence: "fixture",
    },
    {
      id: "droid_raw",
      label: "DROID raw (trajectory.h5 + three MP4 cameras)",
      description:
        "Read-only DROID episode folders. LEVI builds an aligned browsing view; training-format conversion requires an explicit mapping.",
      evidence: "real-data",
    },
    {
      id: "lerobot",
      label: "LeRobot dataset",
      description:
        "A converted LeRobot dataset (meta/info.json, parquet, videos).",
      evidence: "real-data",
    },
  ],
  outputs: [
    {
      id: "lerobot_v21",
      label: "LeRobot v2.1 dataset",
    },
    {
      id: "recap_value",
      label: "RECAP value dataset (π*0.6)",
    },
  ],
  unsupported: [
    {
      id: "lerobot_v3_output",
      label: "LeRobot v3.x dataset (output)",
      reason:
        "Conversion writes v2.1 (the layout openpi and RLinf RECAP load). v3 datasets can be browsed and annotated.",
      workaround:
        "Convert the v2.1 output with lerobot's convert_dataset_v21_to_v30 script.",
    },
    {
      id: "lerobot_v3_input",
      label: "LeRobot v3.x dataset (input)",
      reason:
        "Re-export reads per-episode parquet/MP4 files; v3 packs episodes into shared files.",
      workaround:
        "Export from the raw capture, or convert v3 back to v2.1 first.",
    },
    {
      id: "rlds",
      label: "RLDS / Open X-Embodiment",
      reason: "No reader or writer yet.",
      workaround:
        "Convert to LeRobot with lerobot's port scripts, then use LEVI.",
    },
    {
      id: "hdf5",
      label: "Other HDF5 profiles (robomimic / ALOHA)",
      reason:
        "DROID raw is supported for browsing and annotation; other HDF5 schemas are not mapped.",
      workaround:
        "Add a schema-specific InputFormat and view builder (see docs/CONVERSION.md).",
    },
  ],
  matrix: {
    robot_capture: ["lerobot_v21", "recap_value"],
    image_sequence: ["lerobot_v21", "recap_value"],
    droid_raw: [],
    lerobot: ["recap_value"],
  },
};

function serve() {
  globalThis.fetch = mock(() =>
    Promise.resolve(
      new Response(JSON.stringify(FORMATS), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  ) as unknown as typeof fetch;
}

describe("the supported-formats table", () => {
  test("Chinese: no English sentence is left in it", async () => {
    localStorage.setItem("levi-language", "zh");
    serve();
    const { host } = await render(
      <LocaleProvider>
        <FormatsTable />
      </LocaleProvider>,
    );
    await flush(80);
    const cells = [...host.querySelectorAll("td, .pg-small")].map(
      (cell) => cell.textContent ?? "",
    );
    expect(cells.length).toBeGreaterThan(10);
    // Four English words in a row is a sentence nobody translated; file
    // names, commands and format ids are not.
    const untranslated = cells.filter((text) =>
      /\b[A-Za-z]{2,} [a-z]{2,} [a-z]{2,} [a-z]{2,}\b/.test(text),
    );
    expect(untranslated).toEqual([]);
    expect(host.textContent).toContain("只读的 DROID 片段文件夹");
    expect(host.textContent).toContain("其他 HDF5 格式");
  });

  test("English shows the registry's own sentences", async () => {
    serve();
    const { host } = await render(<FormatsTable />);
    await flush(80);
    expect(host.textContent).toContain("No reader or writer yet.");
    expect(host.textContent).toContain(
      "Other HDF5 profiles (robomimic / ALOHA)",
    );
  });
});
