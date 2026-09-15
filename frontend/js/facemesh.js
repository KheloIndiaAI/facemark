/* On-device face landmarks, for the live enrolment overlay.
 *
 * WHY THIS EXISTS
 * The overlay used to get its dots from /api/enroll/pose-check: five points,
 * about three times a second, each one a server round trip. That is enough to
 * prove a face was found and far too slow to look like tracking - the dots step
 * rather than follow. MediaPipe's Face Landmarker runs in the browser at video
 * rate and returns 478 points, which is what makes an overlay feel live.
 *
 * It is the same library and version the KIRTI analyzer uses, loaded the same
 * way (FilesetResolver + a .task model), so the two apps behave alike.
 *
 * EVERYTHING HERE IS OPTIONAL. The runtime is 27 MB fetched at build time by
 * scripts/fetch_frontend_models.py, and that fetch is allowed to fail. Every
 * entry point returns null rather than throwing, and the caller keeps its
 * server-side path. A blocked CDN degrades the overlay; it must not break
 * enrolment.
 *
 * This is a classic script, not a module - the app has no build step - so the
 * library is pulled in with a dynamic import() at first use. Nothing is
 * downloaded until someone actually opens a capture modal.
 */
const FaceMesh = (() => {
    const WASM_DIR = '/vendor/mediapipe/wasm';
    const BUNDLE   = '/vendor/mediapipe/vision_bundle.mjs';
    const MODEL    = '/vendor/mediapipe/face_landmarker.task';

    let loadPromise = null;     // in-flight or settled load
    let landmarker = null;
    let unavailable = false;    // sticky: do not retry a 27 MB download per modal

    /** Is the runtime even present? Cheap probe, so a missing vendor/ never
     *  costs a failed 12 MB request. */
    async function present() {
        try {
            const r = await fetch(BUNDLE, { method: 'HEAD' });
            return r.ok;
        } catch { return false; }
    }

    async function build(delegate) {
        const vision = await import(BUNDLE);
        const fileset = await vision.FilesetResolver.forVisionTasks(WASM_DIR);
        return await vision.FaceLandmarker.createFromOptions(fileset, {
            baseOptions: { modelAssetPath: MODEL, delegate },
            runningMode: 'VIDEO',
            numFaces: 1,
            // Blendshapes are not needed to draw dots and cost extra compute
            // every frame. They are what a blink challenge would use later.
            outputFaceBlendshapes: false,
            // The head's fitted 3D rotation - see pose() for why yaw comes
            // from here rather than from landmark geometry.
            outputFacialTransformationMatrixes: true,
        });
    }

    const DEG = 180 / Math.PI;

    /* Head angles, in the SERVER's sign convention and close to its scale,
     * because the server's check on the finished clip is the one that decides.
     *
     * Measured on 303 frames the server had already labelled (left, right,
     * centre, down), comparing each source against the server's own 5-point
     * estimate on identical pixels:
     *
     *   yaw   - the transformation matrix. On a still face it wobbles a median
     *           2.1 deg between frames (worst tenth 3.7) against the server's
     *           6.3 (10.1); it gives the same left/centre/right answer on 96.5%
     *           of frames, same sign, reading about 0.8x the server's value.
     *           Landmark geometry agreed more closely on average but threw
     *           15-degree spikes on one frame in ten - a false "turned" read.
     *   pitch - the transformation matrix, NEGATED. Its raw sign runs opposite
     *           to the server's (down frames read -20 deg from centre where
     *           the server reads +14), and its absolute value carries a large
     *           per-person bias (centre frames 13.6 +/- 10.7), so it is only
     *           meaningful RELATIVE to that person's own straight-ahead -
     *           which is the only way the guided sequence uses it. Landmark
     *           geometry with the server's own pitch formula was tried and
     *           does not track the server at all (correlation 0.07). */
    function pose(matrix) {
        if (!matrix || !matrix.data) return { yaw: null, pitch: null };
        const d = matrix.data;
        return {
            yaw: Math.atan2(-d[2], Math.hypot(d[0], d[1])) * DEG,
            pitch: -Math.atan2(d[6], d[10]) * DEG,
        };
    }

    /** Load once. Returns the landmarker, or null if unavailable. */
    async function load() {
        if (landmarker) return landmarker;
        if (unavailable) return null;
        if (loadPromise) return loadPromise;

        loadPromise = (async () => {
            if (!(await present())) {
                unavailable = true;
                console.info('[facemesh] vendor assets absent - using server detection');
                return null;
            }
            // GPU first, CPU second. The same fallback KIRTI uses: a device can
            // advertise WebGL and still fail to create the GPU delegate, and a
            // CPU landmarker is far better than no overlay.
            for (const delegate of ['GPU', 'CPU']) {
                try {
                    landmarker = await build(delegate);
                    console.info(`[facemesh] ready (${delegate})`);
                    return landmarker;
                } catch (e) {
                    console.warn(`[facemesh] ${delegate} delegate failed:`, e && e.message);
                }
            }
            unavailable = true;
            return null;
        })();
        return loadPromise;
    }

    /** One video frame: `{ points, yaw, pitch }`, or null when no face.
     *
     *  `points` are [{x, y}] normalised 0..1. `yaw`/`pitch` are degrees (see
     *  pose()) and either may be null if that frame could not supply it.
     *
     *  `tsMs` must increase between calls - detectForVideo rejects a timestamp
     *  that goes backwards, which happens if two callers share one landmarker.
     */
    let lastTs = -1;
    function detect(video, tsMs) {
        if (!landmarker || !video || !video.videoWidth) return null;
        const t = Math.max(tsMs, lastTs + 1);
        lastTs = t;
        try {
            const res = landmarker.detectForVideo(video, t);
            const face = res && res.faceLandmarks && res.faceLandmarks[0];
            if (!face || !face.length) return null;
            const matrix = res.facialTransformationMatrixes && res.facialTransformationMatrixes[0];
            return { points: face, ...pose(matrix) };
        } catch {
            return null;                 // a dropped frame, not a failure
        }
    }

    /** Release the GPU/CPU context. Safe to call when nothing is loaded. */
    function close() {
        try { if (landmarker) landmarker.close(); } catch { /* best effort */ }
        landmarker = null;
        loadPromise = null;
        lastTs = -1;
    }

    return { load, detect, close, get ready() { return !!landmarker; } };
})();
