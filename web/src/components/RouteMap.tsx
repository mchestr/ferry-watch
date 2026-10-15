import { useMemo } from "react";
import { useRouteMap } from "@/lib/hooks";
import type { LabelledStop, View } from "@/lib/route-map";
import type { Terminal } from "@/lib/schema";
import { cn } from "@/lib/utils";

const ROUND = { fill: "none", strokeLinecap: "round", strokeLinejoin: "round" } as const;

/**
 * A small map of the route from `from` to `to`, or of everywhere `from` goes when there's no `to`. It
 * draws from data bundled with the app (no map service sees where anyone's going) and loads it the first
 * time it's shown. Nothing renders if the map doesn't know a terminal.
 */
export function RouteMap({
  from,
  to,
  terminals,
  className,
}: {
  from: string;
  to: string;
  terminals: Terminal[];
  className?: string;
}) {
  const map = useRouteMap();
  const scene = useMemo(
    () => (map.data && from ? map.data.routeScene(map.data.ROUTE_MAP, from, to, terminals) : undefined),
    [map.data, from, to, terminals],
  );

  if (map.isPending && from) {
    return <div aria-hidden className={cn("aspect-[16/9] rounded-xl border bg-map-water", className)} />;
  }
  if (!map.data || !scene) return null;
  const { FRAME, LABEL_SIZE, ROUTE_MAP, coastPath } = map.data;

  return (
    <div className={cn("overflow-hidden rounded-xl border", className)}>
      <svg
        viewBox={`0 0 ${FRAME.width} ${FRAME.height}`}
        role="img"
        aria-label={scene.description}
        className="block h-auto w-full select-none"
      >
        <rect width={FRAME.width} height={FRAME.height} className="fill-map-water" />
        <path
          d={coastPath(ROUTE_MAP)}
          transform={coastTransform(scene.view)}
          className="fill-map-land stroke-map-coast"
          strokeWidth={1}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        {scene.others.map(
          (stop) =>
            stop.route && (
              <path
                key={stop.code}
                d={stop.route}
                {...ROUND}
                className="stroke-primary/40"
                strokeWidth={1.75}
                strokeDasharray="4 4"
              />
            ),
        )}
        {scene.route && (
          <>
            <path d={scene.route} {...ROUND} className="stroke-map-land dark:stroke-map-water" strokeWidth={7} />
            <path d={scene.route} {...ROUND} className="stroke-primary/30" strokeWidth={3} />
            <path
              d={scene.route}
              {...ROUND}
              strokeLinecap="butt"
              className="stroke-primary motion-safe:animate-route-flow"
              strokeWidth={3}
              strokeDasharray="8 5"
            />
          </>
        )}
        {scene.others.map((stop) => (
          <circle
            key={stop.code}
            cx={stop.at[0]}
            cy={stop.at[1]}
            r={3.5}
            className="fill-primary stroke-map-land dark:stroke-map-water"
            strokeWidth={1.5}
          />
        ))}
        <circle
          cx={scene.from.at[0]}
          cy={scene.from.at[1]}
          r={5}
          className="fill-map-land stroke-primary dark:fill-map-water"
          strokeWidth={2.5}
        />
        {scene.to && (
          <circle
            cx={scene.to.at[0]}
            cy={scene.to.at[1]}
            r={5.5}
            className="fill-primary stroke-map-land dark:stroke-map-water"
            strokeWidth={2}
          />
        )}
        <Label stop={scene.from} size={LABEL_SIZE} />
        {scene.to && <Label stop={scene.to} size={LABEL_SIZE} />}
      </svg>
    </div>
  );
}

/** Draws the coast, which is in map points, in the view's screen points. */
function coastTransform(view: View): string {
  return `matrix(${view.scale} 0 0 ${view.scale} ${-view.left * view.scale} ${-view.top * view.scale})`;
}

function Label({ stop, size }: { stop: LabelledStop; size: number }) {
  return (
    <text
      x={stop.label.x}
      y={stop.label.y}
      textAnchor={stop.label.anchor}
      fontSize={size}
      strokeWidth={3.5}
      strokeLinejoin="round"
      className="fill-foreground stroke-background font-semibold [paint-order:stroke]"
    >
      {stop.place}
    </text>
  );
}
