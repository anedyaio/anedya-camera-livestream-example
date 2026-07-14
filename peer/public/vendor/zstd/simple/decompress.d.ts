export type DecompressOption = {
    defaultHeapSize?: number;
};
export declare const decompress: (buf: Uint8Array, opts?: DecompressOption) => Uint8Array;
