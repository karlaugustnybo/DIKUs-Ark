export const FRAME_BUDGET_MS = 1000 / 60;
export const LONG_FRAME_MS = 50;

export type MapPerformanceSample = {
  longitude: number;
  latitude: number;
  zoom: number;
  resolution: 3 | 7;
  fineCoverageReady: boolean;
  fineTilesLoading: boolean;
  fineTileCount: number;
  fineCellCount: number;
  res7RequestCount: number;
  basemapPixelRatio: number;
  cellPixelRatio: number;
};

export type FrameTimeSummary = {
  frameCount: number;
  p95FrameMs: number;
  worstFrameMs: number;
  overBudgetFrameCount: number;
  longFrameCount: number;
};

export type MapPerformanceResult = FrameTimeSummary & {
  sequence: number;
  durationMs: number;
  longTaskCount: number;
  longTaskDurationMs: number;
  uncoveredFrameCount: number;
  resolutionFlashFrameCount: number;
  loadingFrameCount: number;
  res7RequestCount: number;
  cameraTravelDegrees: number;
  zoomTravel: number;
  minFineTileCount: number;
  maxFineTileCount: number;
  minBasemapPixelRatio: number;
  maxBasemapPixelRatio: number;
  minCellPixelRatio: number;
  maxCellPixelRatio: number;
  start: MapPerformanceSample;
  end: MapPerformanceSample;
};

export type MapPerformanceRunAggregate = {
  runCount: number;
  medianP95FrameMs: number;
  worstP95FrameMs: number;
  worstFrameMs: number;
  medianOverBudgetFrameCount: number;
  totalLongFrameCount: number;
  totalLongTaskCount: number;
  totalLongTaskDurationMs: number;
  totalUncoveredFrameCount: number;
  totalResolutionFlashFrameCount: number;
  medianLoadingFrameCount: number;
  medianRes7RequestCount: number;
  minCameraTravelDegrees: number;
  maxCameraTravelDegrees: number;
};

type MapPerformanceMonitorOptions = {
  sample: () => MapPerformanceSample;
  now?: () => number;
  requestFrame?: (callback: FrameRequestCallback) => number;
  cancelFrame?: (handle: number) => void;
  createLongTaskObserver?: (callback: PerformanceObserverCallback) => PerformanceObserver | null;
};

function rounded(value: number): number {
  return Math.round(value * 100) / 100;
}

export function percentile(values: readonly number[], fraction: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const bounded = Math.min(1, Math.max(0, fraction));
  const index = Math.max(0, Math.ceil(bounded * sorted.length) - 1);
  return sorted[index];
}

export function summarizeFrameTimes(frameTimes: readonly number[]): FrameTimeSummary {
  return {
    frameCount: frameTimes.length,
    p95FrameMs: rounded(percentile(frameTimes, 0.95)),
    worstFrameMs: rounded(frameTimes.length ? Math.max(...frameTimes) : 0),
    overBudgetFrameCount: frameTimes.filter((duration) => duration > FRAME_BUDGET_MS).length,
    longFrameCount: frameTimes.filter((duration) => duration > LONG_FRAME_MS).length
  };
}

export function summarizePerformanceRuns(
  results: readonly MapPerformanceResult[]
): MapPerformanceRunAggregate | null {
  if (!results.length) return null;
  const values = (select: (result: MapPerformanceResult) => number) => results.map(select);
  const median = (select: (result: MapPerformanceResult) => number) =>
    rounded(percentile(values(select), 0.5));
  const cameraTravel = values((result) => result.cameraTravelDegrees);
  return {
    runCount: results.length,
    medianP95FrameMs: median((result) => result.p95FrameMs),
    worstP95FrameMs: rounded(Math.max(...values((result) => result.p95FrameMs))),
    worstFrameMs: rounded(Math.max(...values((result) => result.worstFrameMs))),
    medianOverBudgetFrameCount: median((result) => result.overBudgetFrameCount),
    totalLongFrameCount: results.reduce((total, result) => total + result.longFrameCount, 0),
    totalLongTaskCount: results.reduce((total, result) => total + result.longTaskCount, 0),
    totalLongTaskDurationMs: rounded(
      results.reduce((total, result) => total + result.longTaskDurationMs, 0)
    ),
    totalUncoveredFrameCount: results.reduce(
      (total, result) => total + result.uncoveredFrameCount,
      0
    ),
    totalResolutionFlashFrameCount: results.reduce(
      (total, result) => total + result.resolutionFlashFrameCount,
      0
    ),
    medianLoadingFrameCount: median((result) => result.loadingFrameCount),
    medianRes7RequestCount: median((result) => result.res7RequestCount),
    minCameraTravelDegrees: rounded(Math.min(...cameraTravel)),
    maxCameraTravelDegrees: rounded(Math.max(...cameraTravel))
  };
}

export function wrappedLongitudeDistance(start: number, end: number): number {
  const difference = ((end - start + 540) % 360) - 180;
  return Math.abs(difference);
}

export class MapPerformanceMonitor {
  private readonly sample: () => MapPerformanceSample;
  private readonly now: () => number;
  private readonly requestFrame: (callback: FrameRequestCallback) => number;
  private readonly cancelFrame: (handle: number) => void;
  private readonly createLongTaskObserver: (
    callback: PerformanceObserverCallback
  ) => PerformanceObserver | null;

  private active = false;
  private sequence = 0;
  private startedAt = 0;
  private previousFrameAt: number | null = null;
  private frameHandle: number | null = null;
  private frameTimes: number[] = [];
  private startSample: MapPerformanceSample | null = null;
  private previousSample: MapPerformanceSample | null = null;
  private cameraTravelDegrees = 0;
  private zoomTravel = 0;
  private uncoveredFrameCount = 0;
  private resolutionFlashFrameCount = 0;
  private loadingFrameCount = 0;
  private minFineTileCount = 0;
  private maxFineTileCount = 0;
  private minBasemapPixelRatio = 1;
  private maxBasemapPixelRatio = 1;
  private minCellPixelRatio = 1;
  private maxCellPixelRatio = 1;
  private longTaskCount = 0;
  private longTaskDurationMs = 0;
  private longTaskObserver: PerformanceObserver | null = null;

  constructor(options: MapPerformanceMonitorOptions) {
    this.sample = options.sample;
    this.now = options.now ?? (() => performance.now());
    this.requestFrame = options.requestFrame ?? ((callback) => requestAnimationFrame(callback));
    this.cancelFrame = options.cancelFrame ?? ((handle) => cancelAnimationFrame(handle));
    this.createLongTaskObserver = options.createLongTaskObserver ?? ((callback) => {
      if (typeof PerformanceObserver === 'undefined') return null;
      try {
        const observer = new PerformanceObserver(callback);
        observer.observe({ entryTypes: ['longtask'] });
        return observer;
      } catch {
        return null;
      }
    });
  }

  start(): void {
    if (this.active) return;
    this.active = true;
    this.startedAt = this.now();
    this.previousFrameAt = null;
    this.frameTimes = [];
    this.startSample = this.sample();
    this.previousSample = this.startSample;
    this.cameraTravelDegrees = 0;
    this.zoomTravel = 0;
    this.uncoveredFrameCount = 0;
    this.resolutionFlashFrameCount = 0;
    this.loadingFrameCount = 0;
    this.minFineTileCount = this.startSample.fineTileCount;
    this.maxFineTileCount = this.startSample.fineTileCount;
    this.minBasemapPixelRatio = this.startSample.basemapPixelRatio;
    this.maxBasemapPixelRatio = this.startSample.basemapPixelRatio;
    this.minCellPixelRatio = this.startSample.cellPixelRatio;
    this.maxCellPixelRatio = this.startSample.cellPixelRatio;
    this.longTaskCount = 0;
    this.longTaskDurationMs = 0;
    this.longTaskObserver = this.createLongTaskObserver((list) => {
      const entries = list.getEntries();
      this.longTaskCount += entries.length;
      this.longTaskDurationMs += entries.reduce((total, entry) => total + entry.duration, 0);
    });
    this.frameHandle = this.requestFrame(this.onFrame);
  }

  stop(): MapPerformanceResult | null {
    if (!this.active || !this.startSample) return null;
    this.active = false;
    if (this.frameHandle !== null) this.cancelFrame(this.frameHandle);
    this.frameHandle = null;
    this.longTaskObserver?.disconnect();
    this.longTaskObserver = null;
    const endedAt = this.now();
    const endSample = this.sample();
    this.observeSample(endSample);
    const res7RequestCount = Math.max(
      0,
      endSample.res7RequestCount - this.startSample.res7RequestCount
    );
    this.sequence += 1;
    return {
      sequence: this.sequence,
      durationMs: rounded(Math.max(0, endedAt - this.startedAt)),
      ...summarizeFrameTimes(this.frameTimes),
      longTaskCount: this.longTaskCount,
      longTaskDurationMs: rounded(this.longTaskDurationMs),
      uncoveredFrameCount: this.uncoveredFrameCount,
      resolutionFlashFrameCount: this.resolutionFlashFrameCount,
      loadingFrameCount: this.loadingFrameCount,
      res7RequestCount,
      cameraTravelDegrees: rounded(this.cameraTravelDegrees),
      zoomTravel: rounded(this.zoomTravel),
      minFineTileCount: this.minFineTileCount,
      maxFineTileCount: this.maxFineTileCount,
      minBasemapPixelRatio: this.minBasemapPixelRatio,
      maxBasemapPixelRatio: this.maxBasemapPixelRatio,
      minCellPixelRatio: this.minCellPixelRatio,
      maxCellPixelRatio: this.maxCellPixelRatio,
      start: this.startSample,
      end: endSample
    };
  }

  dispose(): void {
    this.active = false;
    if (this.frameHandle !== null) this.cancelFrame(this.frameHandle);
    this.frameHandle = null;
    this.longTaskObserver?.disconnect();
    this.longTaskObserver = null;
  }

  private readonly onFrame: FrameRequestCallback = (timestamp) => {
    if (!this.active) return;
    if (this.previousFrameAt !== null) this.frameTimes.push(timestamp - this.previousFrameAt);
    this.previousFrameAt = timestamp;
    this.observeSample(this.sample());
    this.frameHandle = this.requestFrame(this.onFrame);
  };

  private observeSample(sample: MapPerformanceSample): void {
    if (sample.resolution === 7 && !sample.fineCoverageReady) this.uncoveredFrameCount += 1;
    if (this.startSample?.resolution === 7 && sample.resolution === 3) {
      this.resolutionFlashFrameCount += 1;
    }
    if (sample.fineTilesLoading) this.loadingFrameCount += 1;
    this.minFineTileCount = Math.min(this.minFineTileCount, sample.fineTileCount);
    this.maxFineTileCount = Math.max(this.maxFineTileCount, sample.fineTileCount);
    this.minBasemapPixelRatio = Math.min(
      this.minBasemapPixelRatio,
      sample.basemapPixelRatio
    );
    this.maxBasemapPixelRatio = Math.max(
      this.maxBasemapPixelRatio,
      sample.basemapPixelRatio
    );
    this.minCellPixelRatio = Math.min(this.minCellPixelRatio, sample.cellPixelRatio);
    this.maxCellPixelRatio = Math.max(this.maxCellPixelRatio, sample.cellPixelRatio);
    if (this.previousSample) {
      this.cameraTravelDegrees += Math.hypot(
        wrappedLongitudeDistance(this.previousSample.longitude, sample.longitude),
        sample.latitude - this.previousSample.latitude
      );
      this.zoomTravel += Math.abs(sample.zoom - this.previousSample.zoom);
    }
    this.previousSample = sample;
  }
}
