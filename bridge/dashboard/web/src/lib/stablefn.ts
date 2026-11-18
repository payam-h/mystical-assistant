import { useCallback, useRef } from "react";

/* A handler whose identity never changes but which always runs the newest closure
   (React's proposed useEffectEvent, done by hand).

   Why: App declares the transcript's handlers (runCommand, quote, openFileRef,
   respond, answerAsk) as plain functions, so every App render — each 1.5–5 s poll,
   every keystroke in the prompt box — handed RunStream new props. That defeated
   RunStream's memo and mergeDelta's turn-identity work: every visible turn
   re-rendered and every Markdown block in it re-parsed, a few times a second, for
   as long as the window was open (measured: ~3 RunStream and ~33 Markdown renders
   per App render on a 3-turn tail, 8.5k events).

   Why not useCallback with deps: the handlers read a dozen pieces of App state
   (draft, selected, sessionProject, turns…); listing them would re-create the
   function on exactly the renders we want to skip, and missing one is a stale bug.
   The ref is written during render, the same pattern Markdown's latestOpen uses —
   a render React throws away can leave it pointing one closure ahead, which for an
   event handler is harmless. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function useStableFn<F extends (...args: any[]) => any>(fn: F): F {
  const latest = useRef(fn);
  latest.current = fn;
  return useCallback(((...args: Parameters<F>) => latest.current(...args)) as F, []);
}
