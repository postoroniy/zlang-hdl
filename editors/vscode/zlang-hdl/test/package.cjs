// SPDX-License-Identifier: Apache-2.0
// Build a reviewed local VSIX with one bundled runtime and no node_modules tree.
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createVSIX } = require('@vscode/vsce');
const { buildBundle, extension, thirdPartyNotices } = require('./bundle.cjs');

const repository = path.resolve(extension, '../../..');
const staticAssets = [
  'README.md', 'CHANGELOG.md', 'language-configuration.json', 'recommended-settings.json',
  'syntaxes/zlang.tmLanguage.json', 'snippets/zlang-hdl.json',
];

async function main() {
  const targetIndex = process.argv.indexOf('--target');
  const target = targetIndex === -1 ? null : process.argv[targetIndex + 1];
  const nativeIndex=process.argv.indexOf('--development-native-root');
  const developmentRoot=nativeIndex===-1?null:process.argv[nativeIndex+1];
  const positional = process.argv.slice(2).filter((item, index, values) => (
    !['--target','--development-native-root'].includes(item)
    && !['--target','--development-native-root'].includes(values[index-1])
  ));
  if (positional.length !== 1 || !path.isAbsolute(positional[0]) || !positional[0].endsWith('.vsix')) {
    throw new Error('Usage: npm run package[:linux-x64] -- /absolute/output/zlang-hdl.vsix');
  }
  if (target !== null && target !== 'linux-x64') throw new Error(`Unsupported VSIX target: ${target}`);
  const output = path.resolve(positional[0]);
  if(developmentRoot!==null){
    const root=fs.realpathSync(developmentRoot);
    const relative=path.relative(repository,root);
    if(target!=='linux-x64'||relative.startsWith('..')||path.isAbsolute(relative)
      ||!['native-runtime/target/','build/tmp/'].some(prefix=>relative.replaceAll('\\','/').startsWith(prefix))
      ||!output.startsWith(path.join(repository,'build','tmp')+path.sep))
      throw new Error('Development native tools and VSIX must remain in the checkout build directories');
  }
  const reservation = fs.openSync(output, 'wx');
  fs.closeSync(reservation);
  const scratch = path.join(repository, 'build', 'tmp');
  fs.mkdirSync(scratch, { recursive: true });
  const stage = fs.mkdtempSync(path.join(scratch, 'zlang-editor-package-'));
  try {
    for (const relative of staticAssets) {
      const source = path.join(extension, relative);
      if (!fs.lstatSync(source).isFile()) throw new Error(`Not a regular asset: ${relative}`);
      const destination = path.join(stage, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.copyFileSync(source, destination);
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'package.json'), 'utf8'));
    if (target === null) {
      delete manifest.contributes.breakpoints;
      delete manifest.contributes.debuggers;
      delete manifest.contributes.customEditors;
      delete manifest.contributes.menus;
      delete manifest.contributes.keybindings;
      manifest.contributes.commands = [];
      delete manifest.contributes.configuration.properties['zlang.waveform.wcp.enabled'];
      delete manifest.contributes.configuration.properties['zlang.waveform.wcp.port'];
      manifest.categories = manifest.categories.filter((item) => item === 'Programming Languages');
    } else {
      const binaryRoot = developmentRoot===null
        ? path.resolve(repository, 'release/native-editor', target) : fs.realpathSync(developmentRoot);
      const destinationRoot = path.join(stage, 'bin', target);
      fs.mkdirSync(destinationRoot, { recursive: true });
      for (const name of ['zlang-debug-adapter', 'zlang-waveform-indexer']) {
        const source = path.join(binaryRoot, name);
        if (!isReviewedExecutable(source)) throw new Error(`Missing reviewed native tool: ${source}`);
        if(developmentRoot===null){
          const status=JSON.parse(fs.readFileSync(path.join(repository,'release/status.json'),'utf8'));
          const record=status.native_editor.executables.find(item=>item.name===name);
          const bytes=fs.readFileSync(source);
          if(!record||record.size!==bytes.length
            ||record.sha256!==crypto.createHash('sha256').update(bytes).digest('hex'))
            throw new Error(`Native tool differs from reviewed release identity: ${name}`);
        }
        fs.copyFileSync(source, path.join(destinationRoot, name));
        fs.chmodSync(path.join(destinationRoot, name), 0o755);
      }
      fs.copyFileSync(
        path.resolve(repository, 'release/native-editor/THIRD_PARTY_LICENSES.md'),
        path.join(stage, 'THIRD_PARTY_NOTICES_NATIVE.txt'),
      );
    }
    manifest.main = './dist/extension.js';
    if(developmentRoot!==null)manifest.zlangDevelopmentNativeBuild=true;
    delete manifest.files;
    delete manifest.devDependencies;
    delete manifest.scripts;
    fs.writeFileSync(path.join(stage, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`);
    const bundle = await buildBundle();
    fs.mkdirSync(path.join(stage, 'dist'));
    fs.writeFileSync(path.join(stage, 'dist', 'extension.js'), bundle.bytes);
    fs.writeFileSync(path.join(stage, 'dist', 'waveform.js'), bundle.waveform);
    fs.writeFileSync(path.join(stage, 'THIRD_PARTY_NOTICES.txt'), thirdPartyNotices());
    for (const relative of ['LICENSE', 'NOTICE']) {
      fs.copyFileSync(path.join(repository, relative), path.join(stage, relative));
    }
    fs.writeFileSync(path.join(stage, '.vscodeignore'), [
      'extension.js', 'package-lock.json', 'editor-toolchain.json', 'test/**', 'examples/**',
      'node_modules/**', '**/*.map', '**/*.ts', '**/*.d.ts',
    ].join('\n') + '\n');
    await createVSIX({ cwd: stage, packagePath: output, dependencies: false, target: target ?? undefined });
    const bytes = fs.readFileSync(output);
    const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    console.log(`SHA256 ${sha256}  ${output}`);
    console.log('Packaged locally. Nothing was published. Audit the VSIX before installation.');
  } catch (error) {
    fs.unlinkSync(output);
    throw error;
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
}

function isReviewedExecutable(candidate) {
  try {
    const stat = fs.lstatSync(candidate);
    return stat.isFile() && (stat.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
