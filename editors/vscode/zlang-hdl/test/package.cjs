// SPDX-License-Identifier: Apache-2.0
// Package static assets only. Never execute/publish compiler or extension code.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createVSIX } = require('@vscode/vsce');

const extension = path.resolve(__dirname, '..');
const repository = path.resolve(extension, '../../..');
const assets = [
    'package.json', 'README.md', 'CHANGELOG.md',
    'language-configuration.json', 'recommended-settings.json',
    'syntaxes/zlang.tmLanguage.json', 'snippets/zlang-hdl.json',
];

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
        for (const relative of assets) {
            const source = path.join(extension, relative);
            if (!fs.lstatSync(source).isFile()) throw new Error(`Not a regular asset: ${relative}`);
            const destination = path.join(stage, relative);
            fs.mkdirSync(path.dirname(destination), { recursive: true });
            fs.copyFileSync(source, destination);
        }
        // Keep one authoritative license/notice in the source tree.
        for (const relative of ['LICENSE', 'NOTICE']) {
            fs.copyFileSync(path.join(repository, relative), path.join(stage, relative));
        }
        await createVSIX({ cwd: stage, packagePath: output, dependencies: false });
        const sha256 = crypto.createHash('sha256').update(fs.readFileSync(output)).digest('hex');
        console.log(`SHA256 ${sha256}  ${output}`);
        console.log('Packaged locally. Nothing was published. Audit the VSIX before installation.');
    } catch (error) {
        fs.unlinkSync(output);
        throw error;
    } finally {
        // Only this tool-created isolated staging directory is removed.
        fs.rmSync(stage, { recursive: true, force: true });
    }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
