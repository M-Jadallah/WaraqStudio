import { cp, mkdir } from 'node:fs/promises';
await mkdir('dist/src',{recursive:true});
await cp('vendor/dist','dist/vendor',{recursive:true});
await cp('src','dist/src',{recursive:true});
await cp('vendor/LICENSE','dist/LICENSE');
