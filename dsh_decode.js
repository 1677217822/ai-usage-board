// dsh_decode.js — 把 dsh 的 session.jsonl.zstd（多帧拼接容器）解码成明文 JSONL 写到 stdout
// 用法: node dsh_decode.js <文件路径>
// 分帧逻辑移植自 @deepseek-ai/dsh-session-persistence-jsonl 的 scanZstdFrames
const fs = require("fs");
const z = require("node:zlib");

const ZSTD_MAGIC = 4247762216;

function scanFrames(buffer) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) break;
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) break;
    offset += 4;
    if (offset === buffer.length) break;
    const descriptor = buffer.readUInt8(offset);
    offset += 1;
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 32) !== 0;
    const checksum = (descriptor & 4) !== 0;
    const dictionaryFlag = descriptor & 3;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : 1 << contentSizeFlag;
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeaderBytes) break;
    offset += remainingHeaderBytes;
    let complete = true;
    for (;;) {
      if (buffer.length - offset < 3) { complete = false; break; }
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = (blockHeader >>> 1) & 3;
      const blockSize = blockHeader >>> 3;
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) { complete = false; break; }
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (!complete) break;
    if (checksum) {
      if (buffer.length - offset < 4) break;
      offset += 4;
    }
    frames.push([start, offset]);
  }
  return frames;
}

const file = process.argv[2];
const buf = fs.readFileSync(file);
process.stdout.on("error", (e) => { if (e.code === "EPIPE") process.exit(0); throw e; });
for (const [start, end] of scanFrames(buf)) {
  try {
    process.stdout.write(z.zstdDecompressSync(buf.subarray(start, end)));
  } catch (e) {
    // 跳过损坏帧
  }
}
