import { useEffect, useRef, useState, useImperativeHandle, forwardRef } from 'react';
import * as PIXI from 'pixi.js';
// `@pixi/unsafe-eval` replaces PIXI's eval-based shader compiler with one
// that doesn't need `unsafe-eval` in the CSP. Required for Tauri 2 / browser
// extensions / any strict-CSP environment. Must run BEFORE the first PIXI
// Application is constructed.
import { install as installUnsafeEvalShim } from '@pixi/unsafe-eval';
import type { LipSyncDataProto } from './useAvatarSocket';

installUnsafeEvalShim(PIXI);

// Required by pixi-live2d-display
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).PIXI = PIXI;

// Auto-pick Cubism 2 vs Cubism 4 by sniffing the model manifest.
// Cubism 4 files end in `.model3.json` and reference a `.moc3` mesh.
// Cubism 2 files are usually `model.json` / `*model*.json` referencing a `.moc`.
// `pixi-live2d-display` ships separate entry points for each. Return type
// is widened to `any` because the union of the two Live2DModel types loses
// the inherited PIXI.Container properties (width/height/scale/x/y) we use.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function loadModel(modelUrl: string): Promise<any> {
  const isCubism4 = modelUrl.toLowerCase().endsWith('.model3.json');
  if (isCubism4) {
    const mod = await import('pixi-live2d-display/cubism4');
    return mod.Live2DModel.from(modelUrl, { autoInteract: false });
  } else {
    const mod = await import('pixi-live2d-display/cubism2');
    return mod.Live2DModel.from(modelUrl, { autoInteract: false });
  }
}

export interface Live2DViewerHandle {
  setExpression: (name: string) => void;
  playMotion: (group: string, index: number) => void;
}

export interface ModelActions {
  expressions: { name: string }[];
  motions: { group: string; index: number }[];
}

interface Live2DViewerProps {
  modelUrl: string;
  scale: number;
  anchor: string;
  defaultExpression: string;
  lipSyncData?: LipSyncDataProto | null;
  isPlaying: boolean;
  onActionsReady?: (actions: ModelActions) => void;
  /**
   * User-adjustable transform overrides applied on top of the auto-fit.
   * scaleMultiplier=1 means "use auto-fit"; >1 zooms in, <1 zooms out.
   * offsetX/Y are pixels relative to the auto-fit center.
   */
  scaleMultiplier?: number;
  offsetX?: number;
  offsetY?: number;
}

const Live2DViewer = forwardRef<Live2DViewerHandle, Live2DViewerProps>(({
  modelUrl,
  defaultExpression,
  lipSyncData,
  isPlaying,
  onActionsReady,
  scaleMultiplier = 1,
  offsetX = 0,
  offsetY = 0,
}, ref) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const appRef = useRef<PIXI.Application | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const modelRef = useRef<any>(null);
  const animFrameRef = useRef<number>(0);
  const startTimeRef = useRef<number>(0);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useImperativeHandle(ref, () => ({
    setExpression: (name: string) => {
      const model = modelRef.current;
      if (!model) return;
      try {
        model.expression(name);
      } catch (e) {
        console.warn('[Live2DViewer] Expression failed:', e);
      }
    },
    playMotion: (group: string, index: number) => {
      const model = modelRef.current;
      if (!model) return;
      try {
        model.motion(group, index);
      } catch (e) {
        console.warn('[Live2DViewer] Motion failed:', e);
      }
    },
  }));

  // Initialize PIXI application
  useEffect(() => {
    if (!canvasRef.current) return;

    try {
      const app = new PIXI.Application({
        view: canvasRef.current,
        backgroundAlpha: 0,
        autoStart: true,
        resizeTo: canvasRef.current.parentElement ?? undefined,
      });
      // Silence pixi-live2d-display@0.4.0's "isInteractive is not a
      // function" pointer-event spam: that library targets an older
      // PIXI v7 event API. We never need PIXI to do hit-testing on
      // the model (interaction lives in our React UI), so just turn
      // the event system off entirely.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const stage = app.stage as any;
      stage.interactive = false;
      stage.interactiveChildren = false;
      stage.eventMode = 'none';
      appRef.current = app;
    } catch (e) {
      setError(`PIXI init failed: ${e}`);
    }

    return () => {
      appRef.current?.destroy(true, { children: true });
      appRef.current = null;
    };
  }, []);

  // Load Live2D model
  useEffect(() => {
    if (!appRef.current || !modelUrl) return;

    let cancelled = false;
    setError(null);

    (async () => {
      try {
        const model = await loadModel(modelUrl);
        if (cancelled || !appRef.current) return;

        // Stash the auto-fit transform so user-adjustable scale/offset
        // can be re-applied without reloading the model. The second
        // useEffect below reads `model.userData.autoFit` to recompute
        // the live transform.
        const fitModel = () => {
          const appW = appRef.current!.screen.width;
          const appH = appRef.current!.screen.height;
          const scaleX = appW / model.width;
          const scaleY = appH / model.height;
          const fitScale = Math.min(scaleX, scaleY) * 0.9;
          model.userData = model.userData || {};
          model.userData.autoFit = { fitScale, appW, appH };
        };
        fitModel();

        // Disable interaction on the model and all its children so
        // PIXI's event system doesn't walk into Live2D nodes that
        // don't implement the v7+ interactive API (causes the
        // "t.isInteractive is not a function" console spam).
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const m = model as any;
        m.interactive = false;
        m.interactiveChildren = false;
        m.eventMode = 'none';

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (appRef.current.stage as any).addChild(model);
        modelRef.current = model;
        setLoaded(true);

        // Discover available expressions and motions from the model
        const actions: ModelActions = { expressions: [], motions: [] };
        try {
          const settings = model.internalModel?.settings as any;
          // Expressions
          if (settings?.expressions) {
            for (const expr of settings.expressions) {
              actions.expressions.push({ name: expr.name || expr.Name || String(expr.file) });
            }
          }
          // Motions
          if (settings?.motions) {
            for (const [group, motionList] of Object.entries(settings.motions)) {
              if (Array.isArray(motionList)) {
                for (let i = 0; i < motionList.length; i++) {
                  actions.motions.push({ group, index: i });
                }
              }
            }
          }
        } catch (e) {
          console.warn('[Live2DViewer] Could not read model actions:', e);
        }
        onActionsReady?.(actions);

        // Apply default expression
        if (defaultExpression) {
          try { model.expression(defaultExpression); } catch {}
        }
      } catch (err) {
        if (!cancelled) {
          setError(`Failed to load model: ${err}`);
        }
      }
    })();

    return () => {
      cancelled = true;
      if (modelRef.current && appRef.current) {
        appRef.current.stage.removeChild(modelRef.current);
        modelRef.current = null;
      }
      setLoaded(false);
    };
  }, [modelUrl]);

  // Apply user-adjustable transform whenever it changes. We also
  // re-apply on resize via a short ticker so the model stays centered
  // when the canvas grows/shrinks.
  useEffect(() => {
    const applyTransform = () => {
      const model = modelRef.current;
      const app = appRef.current;
      if (!model || !app) return;
      const fit = model.userData?.autoFit;
      if (!fit) return;
      // Recompute fit if app size changed (window resize, panel toggle).
      if (fit.appW !== app.screen.width || fit.appH !== app.screen.height) {
        const scaleX = app.screen.width / model.width;
        const scaleY = app.screen.height / model.height;
        fit.fitScale = Math.min(scaleX, scaleY) * 0.9;
        fit.appW = app.screen.width;
        fit.appH = app.screen.height;
      }
      const finalScale = fit.fitScale * scaleMultiplier;
      // Set scale first so model.width reflects the new size.
      model.scale.set(finalScale);
      const cx = (fit.appW - model.width) / 2;
      const cy = (fit.appH - model.height) / 2;
      model.x = cx + offsetX;
      model.y = cy + offsetY;
    };
    applyTransform();
    // Cheap re-check loop — handles "model just loaded" and "canvas resized"
    // without setting up a ResizeObserver.
    const id = setInterval(applyTransform, 250);
    return () => clearInterval(id);
  }, [scaleMultiplier, offsetX, offsetY, loaded]);

  // Drive lip sync animation
  useEffect(() => {
    if (!modelRef.current || !lipSyncData || !isPlaying) return;
    if (lipSyncData.frames.length === 0) return;

    const frames = lipSyncData.frames;
    const frameDuration = lipSyncData.frame_duration_ms;
    startTimeRef.current = performance.now();

    const animate = () => {
      const elapsed = performance.now() - startTimeRef.current;
      let frameIdx = 0;
      for (let i = 0; i < frames.length; i++) {
        if (frames[i]?.t ?? 0 <= elapsed) {
          frameIdx = i;
        } else {
          break;
        }
      }
      const frame = frames[frameIdx];
      if (frame) {
        try {
          const coreModel = modelRef.current?.internalModel?.coreModel;
          const paramIds = coreModel?._model?.parameters?.ids;
          if (paramIds) {
            for (let i = 0; i < paramIds.length; i++) {
              if (paramIds[i] === 'ParamMouthOpenY') {
                coreModel._model.parameters.values[i] = frame.o;
                break;
              }
            }
          }
        } catch {}
      }
      const lastFrame = frames[frames.length - 1];
      if (lastFrame && elapsed < lastFrame.t + frameDuration * 2) {
        animFrameRef.current = requestAnimationFrame(animate);
      }
    };
    animFrameRef.current = requestAnimationFrame(animate);

    return () => {
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, [lipSyncData, isPlaying]);

  if (error) {
    return (
      <div className="flex items-center justify-center h-full text-red-400">
        <p>{error}</p>
      </div>
    );
  }

  return (
    <div className="relative w-full h-full">
      <canvas ref={canvasRef} className="w-full h-full" style={{ imageRendering: 'auto' }} />
      {!loaded && !error && (
        <div className="absolute inset-0 flex items-center justify-center text-gray-400">
          <p className="animate-pulse">Loading Live2D model...</p>
        </div>
      )}
    </div>
  );
});

Live2DViewer.displayName = 'Live2DViewer';
export default Live2DViewer;
