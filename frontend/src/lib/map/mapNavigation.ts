import { goto } from '$app/navigation';
import { preloadMapBundle } from './mapBundle';

/**
 * Keep the first map navigation on the current page until the shared global
 * res3 snapshot is parsed. Subsequent navigations reuse the same promise and
 * complete immediately.
 */
export function navigateToPreloadedMap(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey ||
    event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  void preloadMapBundle()
    .catch(() => null)
    .then(() => goto('/map'));
}
