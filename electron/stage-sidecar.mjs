import { copyFile, mkdir, access } from 'node:fs/promises';
import path from 'node:path';
const root = path.resolve(import.meta.dirname, '..');
const source = path.join(root, 'dist/controller/easy-comfy-controller.exe');
await access(source);
await mkdir(path.join(root, 'src-tauri/binaries'), { recursive: true });
await copyFile(source, path.join(root, 'src-tauri/binaries/easy-comfy-controller-x86_64-pc-windows-msvc.exe'));
