// SPDX-License-Identifier: Apache-2.0
// Build the local VSIX with only the extension and its production LSP client.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createVSIX } = require('@vscode/vsce');

const extension = path.resolve(__dirname, '..');
const repository = path.resolve(extension, '../../..');
const staticAssets = [
    'package.json', 'README.md', 'CHANGELOG.md', 'extension.js',
    'language-configuration.json', 'recommended-settings.json',
    'syntaxes/zlang.tmLanguage.json', 'snippets/zlang-hdl.json',
];

function copyProductionDependencies(stage) {
    const lock = JSON.parse(fs.readFileSync(path.join(extension, 'package-lock.json'), 'utf8'));
    for (const [packagePath, metadata] of Object.entries(lock.packages)) {
        if (!packagePath.startsWith('node_modules/') || metadata.dev === true) continue;
        const relative = packagePath.slice('node_modules/'.length);
        const source = path.join(extension, 'node_modules', relative);
        const destination = path.join(stage, 'vendor', 'node_modules', relative);
        if (!fs.existsSync(source)) throw new Error(`Missing production dependency: ${packagePath}`);
        fs.mkdirSync(path.dirname(destination), { recursive: true });
        fs.cpSync(source, destination, { recursive: true, dereference: false });
    }
}

async function main() {
    if (process.argv.length !== 3 || !process.argv[2].endsWith('.vsix')) {
        throw new Error('Usage: npm run package -- /absolute/output/zlang-hdl-0.1.0.vsix');
    }
    const output = path.resolve(process.argv[2]);
    // Fail rather than replacing an already reviewed artifact.
    const reservation = fs.openSync(output, 'wx');
    fs.closeSync(reservation);
    const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'zlang-editor-package-'));
    try {
        for (const relative of staticAssets) {
            const source = path.join(extension, relative);
            if (!fs.lstatSync(source).isFile()) throw new Error(`Not a regular asset: ${relative}`);
            const destination = path.join(stage, relative);
            fs.mkdirSync(path.dirname(destination), { recursive: true });
            if (relative === 'package.json') {
                const packageJson = JSON.parse(fs.readFileSync(source, 'utf8'));
                delete packageJson.files;
                delete packageJson.devDependencies;
                delete packageJson.scripts;
                fs.writeFileSync(destination, `${JSON.stringify(packageJson, null, 2)}\n`);
            } else {
                fs.copyFileSync(source, destination);
            }
        }
        fs.copyFileSync(path.join(extension, 'package-lock.json'), path.join(stage, 'package-lock.json'));
        for (const relative of ['LICENSE', 'NOTICE']) {
            fs.copyFileSync(path.join(repository, relative), path.join(stage, relative));
        }
        copyProductionDependencies(stage);
        fs.writeFileSync(path.join(stage, '.vscodeignore'), [
            'package-lock.json', 'test/**', 'examples/**', 'node_modules/**/README.md',
        ].join('\n') + '\n');
        // The temporary stage contains only the extension payload and the
        // production language-client dependencies.  No compiler, repository,
        // test, or development dependency is copied into the VSIX.
        await createVSIX({ cwd: stage, packagePath: output, dependencies: false });
        const sha256 = crypto.createHash('sha256').update(fs.readFileSync(output)).digest('hex');
        console.log(`SHA256 ${sha256}  ${output}`);
        console.log('Packaged locally. Nothing was published. Audit the VSIX before installation.');
    } catch (error) {
        fs.unlinkSync(output);
        throw error;
    } finally {
        fs.rmSync(stage, { recursive: true, force: true });
    }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
