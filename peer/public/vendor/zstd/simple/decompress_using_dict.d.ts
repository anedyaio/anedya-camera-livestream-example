import { DecompressOption } from './decompress';
export declare const createDCtx: () => number;
export declare const freeDCtx: (dctx: number) => any;
export declare const decompressUsingDict: (dctx: number, buf: Uint8Array, dict: Uint8Array, opts?: DecompressOption) => Uint8Array;
