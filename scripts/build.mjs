import { copyFile, cp, mkdir, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const output = new URL("../dist/", import.meta.url);
await rm(output, { recursive: true, force: true });
await cp(new URL("../web/", import.meta.url), output, { recursive: true });
await copyFile(new URL("../variables.css", import.meta.url), new URL("variables.css", output));
await copyFile(new URL("../LICENSE", import.meta.url), new URL("LICENSE.txt", output));
const fonts = [
  ["inter", "inter-latin-wght-normal.woff2"],
  ["inter-tight", "inter-tight-latin-wght-normal.woff2"],
];
await mkdir(new URL("fonts/", output), { recursive: true });
for (const [family, file] of fonts) {
  const root = dirname(require.resolve("@fontsource-variable/" + family + "/package.json"));
  await copyFile(join(root, "files", file), new URL("fonts/" + family + ".woff2", output));
  await copyFile(join(root, "LICENSE"), new URL("fonts/" + family + "-LICENSE.txt", output));
}
