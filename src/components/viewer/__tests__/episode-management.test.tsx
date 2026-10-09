import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";
import {
  EpisodeManagement,
  episodeDeletionDestination,
  type EpisodeDeletionPlan,
  type EpisodeDeletionResult,
  type EpisodeManagementRequest,
} from "../episode-management";

setupDom();

const plan: EpisodeDeletionPlan = {
  confirmation: "snapshot-1",
  episodes: [7],
  files: 5,
  bytes: 2 * 1048576,
  paths: [
    "/rollouts/model/task/demo_0007",
    "/workspace/captures/model__task/demo_0007",
  ],
  shared_files_retained: 3,
};
const result: EpisodeDeletionResult = {
  deleted: true,
  remaining: 2,
  first_episode_index: 2,
  repo_id: "local/live.model__task",
};

function button(host: HTMLElement, text: string): HTMLButtonElement | null {
  return (
    [...host.querySelectorAll<HTMLButtonElement>("button")].find(
      (node) => node.textContent?.trim() === text,
    ) ?? null
  );
}

function checkbox(host: HTMLElement, text: string): HTMLInputElement | null {
  return (
    [...host.querySelectorAll("label")]
      .find(
        (label) =>
          label.querySelector(".ds-choice__label")?.textContent === text,
      )
      ?.querySelector("input") ?? null
  );
}

function recorder(value = result) {
  const calls: {
    method: string;
    path: string;
    body: { episodes: number[]; confirmation?: string };
  }[] = [];
  const request: EpisodeManagementRequest = async <T,>(
    method: "POST" | "DELETE",
    path: string,
    body: unknown,
  ) => {
    const payload = body as { episodes: number[]; confirmation?: string };
    calls.push({ method, path, body: payload });
    return (
      method === "POST" ? { ...plan, episodes: payload.episodes } : value
    ) as T;
  };
  return { calls, request };
}

describe("episode file management", () => {
  test("starts collapsed and selects only the current visible episode when opened", async () => {
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[2, 7, 10]}
        currentEpisode={7}
      />,
    );
    const toggle = button(host, "Manage episode files")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(
      document.getElementById(toggle.getAttribute("aria-controls")!),
    ).not.toBeNull();
    expect(host.querySelector("input")).toBeNull();
    await click(toggle);
    expect(checkbox(host, "Episode 7")?.checked).toBe(true);
    expect(checkbox(host, "Episode 2")?.checked).toBe(false);
    expect(checkbox(host, "Episode 10")?.checked).toBe(false);
    expect(checkbox(host, "Select all visible episodes")?.indeterminate).toBe(
      true,
    );
    expect(host.textContent).toContain("Selected: 1");
  });

  test("one episode is previewed, then separately confirmed with its immutable fingerprint", async () => {
    const { calls, request } = recorder();
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="live.model__task"
        episodeIds={[2, 7, 10]}
        currentEpisode={7}
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(calls).toEqual([
      {
        method: "POST",
        path: "datasets/live.model__task/episodes/deletion-plan",
        body: { episodes: [7] },
      },
    ]);
    expect(host.querySelector("[role=alertdialog]")).not.toBeNull();
    expect(host.textContent).toContain("original rollout data");
    expect(host.textContent).toContain("This cannot be undone.");
    expect(host.textContent).toContain("5 file(s), 2.00 MiB");
    expect(host.textContent).toContain(
      "Shared data files used by other episodes will be kept: 3.",
    );
    expect(host.textContent).toContain(plan.paths[0]);
    expect(navigate).not.toHaveBeenCalled();
    await click(button(host, "Delete episodes and local files"));
    expect(calls[1]).toEqual({
      method: "DELETE",
      path: "datasets/live.model__task/episodes",
      body: { episodes: [7], confirmation: "snapshot-1" },
    });
    expect(navigate).toHaveBeenCalledWith("/local/live.model__task/episode_2");
  });

  test("select-all only requests the actual visible IDs and preserves the session scope", async () => {
    const { calls, request } = recorder({
      ...result,
      remaining: 10,
      first_episode_index: 0,
    });
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="model task"
        episodeIds={[10, 2, 10]}
        currentEpisode={2}
        session="session/1"
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(checkbox(host, "Select all visible episodes"));
    await click(button(host, "Preview deletion"));
    expect(calls[0]).toEqual({
      method: "POST",
      path: "datasets/model%20task/episodes/deletion-plan",
      body: { episodes: [2, 10] },
    });
    await click(button(host, "Delete episodes and local files"));
    expect(navigate).toHaveBeenCalledWith(
      "/local/live.model__task?live_session=session%2F1",
    );
  });

  test("deleting the last episodes returns to the workbench", async () => {
    const { request } = recorder({
      ...result,
      remaining: 0,
      first_episode_index: null,
    });
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await click(button(host, "Delete episodes and local files"));
    expect(navigate).toHaveBeenCalledWith("/workbench");
  });

  test("deleting only part of a session navigates to its scoped root rather than the global first episode", async () => {
    const { calls, request } = recorder();
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7, 10]}
        currentEpisode={7}
        session="session-1"
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(calls[0].body).toEqual({ episodes: [7] });
    await click(button(host, "Delete episodes and local files"));
    expect(navigate).toHaveBeenCalledWith(
      "/local/live.model__task?live_session=session-1",
    );
  });

  test("pending staging cleanup stays visible until the user explicitly reloads remaining episodes", async () => {
    const { calls, request } = recorder({ ...result, cleanup_pending: true });
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7, 10]}
        currentEpisode={7}
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await click(button(host, "Delete episodes and local files"));
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(host.querySelector("[role=status]")?.textContent).toContain(
      "Dataset metadata was updated, but some staged files still need cleanup.",
    );
    expect(navigate).not.toHaveBeenCalled();
    expect(button(host, "Manage episode files")?.disabled).toBe(true);
    expect(calls).toHaveLength(2);
    await click(button(host, "Reload remaining episodes"));
    expect(navigate).toHaveBeenCalledWith("/local/live.model__task/episode_2");
  });

  test("cancel closes the confirmation without deleting files", async () => {
    const { calls, request } = recorder();
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await click(host.querySelector("[role=alertdialog] button"));
    expect(calls).toHaveLength(1);
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });

  test("empty selection cannot request a preview and shows a reason", async () => {
    const { calls, request } = recorder();
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(checkbox(host, "Episode 7"));
    expect(button(host, "Preview deletion")?.disabled).toBe(true);
    expect(host.textContent).toContain("Select at least one episode.");
    await click(button(host, "Preview deletion"));
    expect(calls).toEqual([]);
  });

  test("no visible episodes disables management with a visible reason", async () => {
    const { host } = await render(
      <EpisodeManagement name="demo" episodeIds={[]} currentEpisode={7} />,
    );
    expect(button(host, "Manage episode files")?.disabled).toBe(true);
    expect(host.textContent).toContain("No visible episodes to delete.");
  });

  test("a busy refusal stays visible and opens no destructive confirmation", async () => {
    const request: EpisodeManagementRequest = async () => {
      throw new Error("An export job uses this dataset");
    };
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "export job",
    );
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });

  test("a changed-data refusal needs a fresh preview before another delete", async () => {
    const confirmations: string[] = [];
    let previewCount = 0;
    const request: EpisodeManagementRequest = async <T,>(
      method: "POST" | "DELETE",
      _path: string,
      body: unknown,
    ) => {
      if (method === "POST")
        return { ...plan, confirmation: `snapshot-${++previewCount}` } as T;
      confirmations.push((body as { confirmation: string }).confirmation);
      if (confirmations.length === 1)
        throw new Error("Data changed since the deletion preview");
      return result as T;
    };
    const navigate = mock((url: string) => void url);
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
        navigate={navigate}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await click(button(host, "Delete episodes and local files"));
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
    await click(button(host, "Preview deletion"));
    await click(button(host, "Delete episodes and local files"));
    expect(confirmations).toEqual(["snapshot-1", "snapshot-2"]);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  test("a preview cannot broaden the selected deletion scope", async () => {
    const request: EpisodeManagementRequest = async <T,>() =>
      ({ ...plan, episodes: [7, 10] }) as T;
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7, 10]}
        currentEpisode={7}
        request={request}
      />,
    );
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "does not match",
    );
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });

  test("a different dataset or a changed visible list invalidates the open preview", async () => {
    const { calls, request } = recorder();
    const props = {
      name: "one",
      episodeIds: [7, 10],
      currentEpisode: 7,
      request,
    };
    const { host, rerender } = await render(<EpisodeManagement {...props} />);
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(host.querySelector("[role=alertdialog]")).not.toBeNull();
    await rerender(<EpisodeManagement {...props} episodeIds={[10]} />);
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(button(host, "Preview deletion")?.disabled).toBe(true);
    await rerender(<EpisodeManagement {...props} name="two" />);
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(
      button(host, "Manage episode files")?.getAttribute("aria-expanded"),
    ).toBe("false");
    expect(calls).toHaveLength(1);
  });

  test("returning A to B to A cannot revive an already shown deletion preview", async () => {
    const { calls, request } = recorder();
    const props = { name: "one", episodeIds: [7], currentEpisode: 7, request };
    const { host, rerender } = await render(<EpisodeManagement {...props} />);
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    expect(host.querySelector("[role=alertdialog]")).not.toBeNull();
    await rerender(<EpisodeManagement {...props} name="two" />);
    await rerender(<EpisodeManagement {...props} />);
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(button(host, "Delete episodes and local files")).toBeNull();
    expect(calls).toHaveLength(1);
    await click(button(host, "Preview deletion"));
    expect(host.querySelector("[role=alertdialog]")).not.toBeNull();
    expect(calls).toHaveLength(2);
    expect(calls.every((call) => call.method === "POST")).toBe(true);
  });

  test("double clicks cannot duplicate pending previews or deletions", async () => {
    let calls = 0;
    let settle: (value: unknown) => void = () => {};
    const request: EpisodeManagementRequest = <T,>() => {
      calls++;
      return new Promise<T>((resolve) => {
        settle = resolve as (value: unknown) => void;
      });
    };
    const { host } = await render(
      <EpisodeManagement
        name="demo"
        episodeIds={[7]}
        currentEpisode={7}
        request={request}
        navigate={() => {}}
      />,
    );
    await click(button(host, "Manage episode files"));
    await act(async () => {
      const trigger = button(host, "Preview deletion")!;
      trigger.click();
      trigger.click();
    });
    expect(calls).toBe(1);
    await act(async () => settle(plan));
    await act(async () => {
      const trigger = button(host, "Delete episodes and local files")!;
      trigger.click();
      trigger.click();
    });
    expect(calls).toBe(2);
    await act(async () => settle(result));
  });

  test("a preview that returns after navigation cannot open a confirmation for the old dataset", async () => {
    let settle: (value: unknown) => void = () => {};
    const request: EpisodeManagementRequest = <T,>() =>
      new Promise<T>((resolve) => {
        settle = resolve as (value: unknown) => void;
      });
    const props = { name: "one", episodeIds: [7], currentEpisode: 7, request };
    const { host, rerender } = await render(<EpisodeManagement {...props} />);
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await rerender(<EpisodeManagement {...props} name="two" />);
    await act(async () => settle(plan));
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "Episode list changed",
    );
  });

  test("a late preview response cannot revive A's confirmation after A to B to A navigation", async () => {
    let calls = 0;
    let settle: (value: unknown) => void = () => {};
    const request: EpisodeManagementRequest = <T,>() => {
      calls++;
      return new Promise<T>((resolve) => {
        settle = resolve as (value: unknown) => void;
      });
    };
    const props = { name: "one", episodeIds: [7], currentEpisode: 7, request };
    const { host, rerender } = await render(<EpisodeManagement {...props} />);
    await click(button(host, "Manage episode files"));
    await click(button(host, "Preview deletion"));
    await rerender(<EpisodeManagement {...props} name="two" />);
    await rerender(<EpisodeManagement {...props} />);
    await act(async () => settle(plan));
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "Episode list changed",
    );
    expect(calls).toBe(1);
  });

  test("checkbox keys stay within management and do not trigger viewer playback", async () => {
    const forwarded = mock(() => {});
    const { host } = await render(
      <EpisodeManagement name="demo" episodeIds={[7]} currentEpisode={7} />,
    );
    await click(button(host, "Manage episode files"));
    window.addEventListener("keydown", forwarded);
    try {
      await press(checkbox(host, "Episode 7"), " ");
      await press(button(host, "Preview deletion"), " ");
      expect(forwarded).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", forwarded);
    }
  });
});

describe("navigation after episode deletion", () => {
  test("keeps a partially remaining live session scoped to its dataset root", () => {
    expect(episodeDeletionDestination(result, "session/1")).toBe(
      "/local/live.model__task?live_session=session%2F1",
    );
  });
  test("uses the viewer's episode-prefixed path when no session is selected", () => {
    expect(episodeDeletionDestination(result)).toBe(
      "/local/live.model__task/episode_2",
    );
  });
  test("uses the dataset root for an emptied session and never opens another session's episode", () => {
    expect(episodeDeletionDestination(result, "session-1", true)).toBe(
      "/local/live.model__task?live_session=session-1",
    );
  });
  test("uses the workbench when no episodes remain or a response names a nonlocal dataset", () => {
    expect(episodeDeletionDestination({ ...result, remaining: 0 })).toBe(
      "/workbench",
    );
    expect(
      episodeDeletionDestination({ ...result, repo_id: "remote/dataset" }),
    ).toBe("/workbench");
  });
});
