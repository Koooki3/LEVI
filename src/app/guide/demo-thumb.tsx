"use client";
import { useEffect, useRef, useState } from "react";
import { Clapperboard } from "lucide-react";
import { Icon } from "@/components/ds";

/**
 * A dataset's first frame. Until a frame has loaded (and for good when the
 * remote file cannot be reached) it is a quiet placeholder with an icon, not
 * a black block that looks like a broken image.
 */
export function DemoThumb({ id }: { id: string }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  // The video can load (or fail) before React has attached its handlers, in
  // which case the events are gone: look at what it has already done.
  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (element.error) setFailed(true);
    else if (element.readyState >= 2) setLoaded(true);
  }, []);
  return (
    <span
      className="levi-guide-demos__media"
      data-state={loaded ? "ready" : failed ? "failed" : "loading"}
    >
      {!loaded && (
        <span className="levi-guide-demos__placeholder" aria-hidden="true">
          <Icon icon={Clapperboard} size="lg" />
        </span>
      )}
      {!failed && (
        <video
          ref={video}
          src={`/api/proxy/datasets/${id}/resolve/main/videos/chunk-000/observation.images.front/episode_000000.mp4#t=0.1`}
          muted
          playsInline
          preload="metadata"
          aria-hidden="true"
          tabIndex={-1}
          onLoadedData={() => setLoaded(true)}
          onError={() => setFailed(true)}
          onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
          onMouseLeave={(e) => e.currentTarget.pause()}
        />
      )}
    </span>
  );
}
