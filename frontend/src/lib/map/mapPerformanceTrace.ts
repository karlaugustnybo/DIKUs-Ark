import type { Map as MapLibreMap } from 'maplibre-gl';
import {
  FRAME_BUDGET_MS,
  percentile,
  summarizePerformanceRuns,
  type MapPerformanceMonitor,
  type MapPerformanceResult,
  type MapPerformanceRunAggregate
} from '$lib/map/mapPerformance';

export type MapPerformanceTraceSpecification = {
  scenario: 'zoom-10-res7-loading-pan';
  center: [number, number];
  zoom: number;
  pitch: number;
  bearing: number;
  panCount: number;
  panPixels: number;
  panDurationMs: number;
  cooldownMs: number;
};

export const ZOOM_10_LOADING_TRACE: MapPerformanceTraceSpecification = {
  scenario: 'zoom-10-res7-loading-pan',
  center: [10, 45],
  zoom: 10,
  pitch: 0,
  bearing: 0,
  panCount: 5,
  panPixels: -330,
  panDurationMs: 300,
  cooldownMs: 450
};

export type MapPerformanceTraceSummary = {
  specification: MapPerformanceTraceSpecification;
  environment: MapPerformanceTraceEnvironment;
  runCount: number;
  firstPathRun: MapPerformanceResult;
  repeatRuns: MapPerformanceRunAggregate | null;
  allRuns: MapPerformanceRunAggregate;
  runs: MapPerformanceResult[];
};

export type MapPerformanceTraceEnvironment = {
  visibilityState: DocumentVisibilityState;
  hasFocus: boolean;
  cadenceSampleCount: number;
  baselineFrameMs: number;
  estimatedRefreshHz: number;
  supports60FpsBudget: boolean;
};

type RunZoom10MapPerformanceTraceOptions = {
  map: MapLibreMap;
  monitor: MapPerformanceMonitor;
  runCount: number;
  isFineGuardReady: () => boolean;
  isDisposed: () => boolean;
  onRunStart?: (runNumber: number) => void;
  onRunComplete?: (result: MapPerformanceResult, runNumber: number) => void;
};

const READY_TIMEOUT_MS = 20_000;
const MOVE_TIMEOUT_MS = 5_000;

function delay(durationMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, durationMs));
}

function nextAnimationFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

function rounded(value: number): number {
  return Math.round(value * 100) / 100;
}

function measureFrameCadence(): Promise<MapPerformanceTraceEnvironment> {
  return new Promise((resolve) => {
    const frameTimes: number[] = [];
    let previousFrameAt: number | null = null;
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timeout);
      const baselineFrameMs = rounded(percentile(frameTimes, 0.5));
      resolve({
        visibilityState: document.visibilityState,
        hasFocus: document.hasFocus(),
        cadenceSampleCount: frameTimes.length,
        baselineFrameMs,
        estimatedRefreshHz: baselineFrameMs ? rounded(1000 / baselineFrameMs) : 0,
        supports60FpsBudget: document.visibilityState === 'visible' &&
          frameTimes.length >= 8 && baselineFrameMs <= FRAME_BUDGET_MS + 2
      });
    };
    const onFrame: FrameRequestCallback = (timestamp) => {
      if (previousFrameAt !== null) frameTimes.push(timestamp - previousFrameAt);
      previousFrameAt = timestamp;
      if (frameTimes.length >= 12) finish();
      else requestAnimationFrame(onFrame);
    };
    const timeout = setTimeout(finish, 2000);
    requestAnimationFrame(onFrame);
  });
}

function throwIfDisposed(isDisposed: () => boolean) {
  if (isDisposed()) throw new Error('Map performance trace was cancelled');
}

async function waitForFineGuard(
  isFineGuardReady: () => boolean,
  isDisposed: () => boolean
) {
  const startedAt = performance.now();
  while (!isFineGuardReady()) {
    throwIfDisposed(isDisposed);
    if (performance.now() - startedAt > READY_TIMEOUT_MS) {
      throw new Error('Timed out waiting for the complete res7 guard at zoom 10');
    }
    await delay(25);
  }
}

function waitForMapIdle(map: MapLibreMap): Promise<void> {
  if (!map.isMoving() && map.areTilesLoaded()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onIdle = () => {
      clearTimeout(timeout);
      resolve();
    };
    const timeout = setTimeout(() => {
      map.off('idle', onIdle);
      reject(new Error('Timed out waiting for the basemap before the performance trace'));
    }, READY_TIMEOUT_MS);
    map.once('idle', onIdle);
  });
}

function runCameraMove(map: MapLibreMap, move: () => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const onMoveEnd = () => {
      clearTimeout(timeout);
      resolve();
    };
    const timeout = setTimeout(() => {
      map.off('moveend', onMoveEnd);
      reject(new Error('Timed out waiting for the deterministic map movement'));
    }, MOVE_TIMEOUT_MS);
    map.once('moveend', onMoveEnd);
    try {
      move();
    } catch (reason) {
      clearTimeout(timeout);
      map.off('moveend', onMoveEnd);
      reject(reason);
    }
  });
}

export async function runZoom10MapPerformanceTrace(
  options: RunZoom10MapPerformanceTraceOptions
): Promise<MapPerformanceTraceSummary> {
  const specification = ZOOM_10_LOADING_TRACE;
  const requestedRunCount = Number.isFinite(options.runCount) ? Math.floor(options.runCount) : 3;
  const runCount = Math.min(10, Math.max(1, requestedRunCount));
  const results: MapPerformanceResult[] = [];
  const environment = await measureFrameCadence();
  let recording = false;

  try {
    for (let runIndex = 0; runIndex < runCount; runIndex += 1) {
      throwIfDisposed(options.isDisposed);
      await runCameraMove(options.map, () => {
        options.map.jumpTo({
          center: specification.center,
          zoom: specification.zoom,
          pitch: specification.pitch,
          bearing: specification.bearing
        });
      });
      await waitForMapIdle(options.map);
      await waitForFineGuard(options.isFineGuardReady, options.isDisposed);
      await nextAnimationFrame();
      await nextAnimationFrame();

      options.onRunStart?.(runIndex + 1);
      options.monitor.start();
      recording = true;
      for (let panIndex = 0; panIndex < specification.panCount; panIndex += 1) {
        throwIfDisposed(options.isDisposed);
        await runCameraMove(options.map, () => {
          options.map.panBy([specification.panPixels, 0], {
            duration: specification.panDurationMs,
            easing: (progress) => progress
          });
        });
      }
      await delay(specification.cooldownMs);
      const result = options.monitor.stop();
      recording = false;
      if (!result) throw new Error('Map performance monitor did not produce a trace result');
      results.push(result);
      options.onRunComplete?.(result, runIndex + 1);
    }
  } catch (reason) {
    if (recording) options.monitor.stop();
    throw reason;
  }

  const allRuns = summarizePerformanceRuns(results);
  if (!allRuns) throw new Error('Map performance trace completed without any runs');
  return {
    specification: { ...specification },
    environment,
    runCount: results.length,
    firstPathRun: results[0],
    repeatRuns: summarizePerformanceRuns(results.slice(1)),
    allRuns,
    runs: results
  };
}
