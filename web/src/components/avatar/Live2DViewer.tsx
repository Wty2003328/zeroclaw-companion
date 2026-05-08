import { useEffect, useRef, useState, useImperativeHandle, forwardRef } from 'react';
import * as PIXI from 'pixi.js';
import { Live2DModel } from 'pixi-live2d-display/cubism4';
import type { LipSyncDataProto } from './useAvatarSocket';

// Required by pixi-live2d-display
(window as any).PIXI = PIXI;

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
}

const Live2DViewer = forwardRef<Live2DViewerHandle, Live2DViewerProps>(({
  modelUrl,
  defaultExpression,
  lipSyncData,
  isPlaying,
  onActionsReady,
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
        const model = await Live2DModel.from(modelUrl, { autoInteract: false });
        if (cancelled || !appRef.current) return;

        // Fit model to container
        const appW = appRef.current.screen.width;
        const appH = appRef.current.screen.height;
        const scaleX = appW / model.width;
        const scaleY = appH / model.height;
        const s = Math.min(scaleX, scaleY) * 0.9;
        model.scale.set(s);
        model.x = (appW - model.width * s) / 2;
        model.y = (appH - model.height * s) / 2;

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
