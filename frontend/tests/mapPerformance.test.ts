// Bun supplies this module at test runtime; the frontend production tsconfig
// intentionally does not include Bun's ambient server-side types.
// @ts-expect-error -- available to `bun test`, excluded from the browser bundle
import { describe, expect, test } from 'bun:test';
import {
  MapPerformanceMonitor,
  percentile,
  summarizeFrameTimes,
  summarizePerformanceRuns,
  wrappedLongitudeDistance,
  type MapPerformanceResult,
  type MapPerformanceSample
} from '../src/lib/map/mapPerformance';

const performanceSample: MapPerformanceSample = {
  longitude: 10,
  latitude: 45,
  zoom: 10,
  resolution: 3,
  fineCoverageReady: true,
  fineTilesLoading: false,
  fineTileCount: 25,
  fineCellCount: 5000,
  res7RequestCount: 0,
  basemapPixelRatio: 1,
  cellPixelRatio: 1
};

function performanceResult(
  overrides: Partial<MapPerformanceResult> = {}
): MapPerformanceResult {
  return {
    sequence: 1,
    durationMs: 2000,
    frameCount: 120,
    p95FrameMs: 17,
    worstFrameMs: 55,
    overBudgetFrameCount: 10,
    longFrameCount: 1,
    longTaskCount: 1,
    longTaskDurationMs: 60,
    uncoveredFrameCount: 0,
    resolutionFlashFrameCount: 0,
    loadingFrameCount: 12,
    res7RequestCount: 8,
    cameraTravelDegrees: 2.3,
    zoomTravel: 0,
    minFineTileCount: 25,
    maxFineTileCount: 36,
    minBasemapPixelRatio: 1,
    maxBasemapPixelRatio: 2,
    minCellPixelRatio: 1,
    maxCellPixelRatio: 2,
    start: performanceSample,
    end: performanceSample,
    ...overrides
  };
}

describe('map performance summaries', () => {
  test('calculates deterministic frame budgets and percentiles', () => {
    expect(percentile([40, 10, 30, 20], 0.5)).toBe(20);
    expect(percentile([40, 10, 30, 20], 0.95)).toBe(40);
    expect(summarizeFrameTimes([10, 17, 51])).toEqual({
      frameCount: 3,
      p95FrameMs: 51,
      worstFrameMs: 51,
      overBudgetFrameCount: 2,
      longFrameCount: 1
    });
  });

  test('measures wrapped camera travel and per-frame coverage', () => {
    let clock = 0;
    let frame: FrameRequestCallback | null = null;
    const runFrame = (timestamp: number) => {
      if (!frame) throw new Error('A frame was not scheduled');
      frame(timestamp);
    };
    let sample: MapPerformanceSample = {
      longitude: 179.9,
      latitude: 45,
      zoom: 11,
      resolution: 7,
      fineCoverageReady: true,
      fineTilesLoading: false,
      fineTileCount: 36,
      fineCellCount: 5200,
      res7RequestCount: 0,
      basemapPixelRatio: 1,
      cellPixelRatio: 1
    };
    const monitor = new MapPerformanceMonitor({
      sample: () => sample,
      now: () => clock,
      requestFrame: (callback) => {
        frame = callback;
        return 1;
      },
      cancelFrame: () => {},
      createLongTaskObserver: () => null
    });

    monitor.start();
    runFrame(0);
    sample = {
      ...sample,
      longitude: -179.9,
      fineCoverageReady: false,
      fineTilesLoading: true,
      fineTileCount: 30,
      res7RequestCount: 1
    };
    runFrame(17);
    sample = { ...sample, resolution: 3, fineTilesLoading: false };
    runFrame(68);
    sample = {
      ...sample,
      resolution: 7,
      fineCoverageReady: true,
      fineTileCount: 36,
      basemapPixelRatio: 2,
      cellPixelRatio: 2
    };
    clock = 100;
    const result = monitor.stop();

    expect(result).not.toBeNull();
    expect(result?.frameCount).toBe(2);
    expect(result?.p95FrameMs).toBe(51);
    expect(result?.uncoveredFrameCount).toBe(1);
    expect(result?.resolutionFlashFrameCount).toBe(1);
    expect(result?.loadingFrameCount).toBe(1);
    expect(result?.res7RequestCount).toBe(1);
    expect(result?.minFineTileCount).toBe(30);
    expect(result?.maxFineTileCount).toBe(36);
    expect(result?.minBasemapPixelRatio).toBe(1);
    expect(result?.maxBasemapPixelRatio).toBe(2);
    expect(result?.minCellPixelRatio).toBe(1);
    expect(result?.maxCellPixelRatio).toBe(2);
    expect(result?.cameraTravelDegrees).toBeCloseTo(0.2, 5);
  });

  test('uses the shortest longitude path across the antimeridian', () => {
    expect(wrappedLongitudeDistance(179.8, -179.7)).toBeCloseTo(0.5, 8);
    expect(wrappedLongitudeDistance(-10, 10)).toBe(20);
  });

  test('separates repeatable run medians from worst-case totals', () => {
    const aggregate = summarizePerformanceRuns([
      performanceResult(),
      performanceResult({
        sequence: 2,
        p95FrameMs: 15,
        worstFrameMs: 40,
        overBudgetFrameCount: 4,
        longFrameCount: 0,
        longTaskCount: 0,
        longTaskDurationMs: 0,
        loadingFrameCount: 0,
        res7RequestCount: 0,
        cameraTravelDegrees: 2.2
      }),
      performanceResult({
        sequence: 3,
        p95FrameMs: 16,
        worstFrameMs: 45,
        overBudgetFrameCount: 6,
        loadingFrameCount: 1,
        res7RequestCount: 0,
        cameraTravelDegrees: 2.4
      })
    ]);

    expect(aggregate).toEqual({
      runCount: 3,
      medianP95FrameMs: 16,
      worstP95FrameMs: 17,
      worstFrameMs: 55,
      medianOverBudgetFrameCount: 6,
      totalLongFrameCount: 2,
      totalLongTaskCount: 2,
      totalLongTaskDurationMs: 120,
      totalUncoveredFrameCount: 0,
      totalResolutionFlashFrameCount: 0,
      medianLoadingFrameCount: 1,
      medianRes7RequestCount: 0,
      minCameraTravelDegrees: 2.2,
      maxCameraTravelDegrees: 2.4
    });
    expect(summarizePerformanceRuns([])).toBeNull();
  });
});
