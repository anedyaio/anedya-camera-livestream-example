export declare const createCCtx: () => number;
export declare const freeCCtx: (cctx: number) => any;
export declare const compressUsingDict: (cctx: number, buf: Uint8Array, dict: Uint8Array, level?: number) => Uint8Array;
