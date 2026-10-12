'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { before, test } = require('node:test');
const textmate = require('vscode-textmate');
const oniguruma = require('vscode-oniguruma');

const extension = path.resolve(__dirname, '..');
let grammar;

before(async () => {
  const wasm = fs.readFileSync(require.resolve('vscode-oniguruma/release/onig.wasm'));
  await oniguruma.loadWASM(wasm);
  const registry = new textmate.Registry({
    onigLib: Promise.resolve({
      createOnigScanner: (patterns) => new oniguruma.OnigScanner(patterns),
      createOnigString: (text) => new oniguruma.OnigString(text),
    }),
    loadGrammar: async (scope) => {
      assert.equal(scope, 'source.zlang');
      const filename = path.join(extension, 'syntaxes', 'zlang.tmLanguage.json');
      return textmate.parseRawGrammar(fs.readFileSync(filename, 'utf8'), filename);
    },
  });
  grammar = await registry.loadGrammar('source.zlang');
  assert.ok(grammar);
});

function tokenize(source) {
  let stack = textmate.INITIAL;
  const lines = source.split('\n').map((text) => {
    const result = grammar.tokenizeLine(text, stack);
    assert.equal(result.stoppedEarly, false, `Incomplete tokenization: ${text}`);
    stack = result.ruleStack;
    return {
      text,
      tokens: result.tokens.map((token) => ({
        start: token.startIndex,
        end: Math.min(token.endIndex, text.length),
        text: text.slice(token.startIndex, token.endIndex),
        scopes: token.scopes,
      })),
    };
  });
  return { lines, stack };
}

function tokenFor(line, text, occurrence = 0) {
  let start = -1;
  for (let index = 0; index <= occurrence; index += 1) {
    start = line.text.indexOf(text, start + 1);
    assert.notEqual(start, -1, `Missing ${JSON.stringify(text)} in ${line.text}`);
  }
  const token = line.tokens.find((item) => item.start <= start && item.end >= start + text.length);
  assert.ok(token, `Split token ${JSON.stringify(text)} in ${line.text}`);
  return token;
}

function expectScope(line, text, scope, occurrence = 0) {
  const token = tokenFor(line, text, occurrence);
  assert.equal(token.scopes[0], 'source.zlang');
  assert.ok(token.scopes.includes(scope), `${text}: ${JSON.stringify(token.scopes)} lacks ${scope}`);
  return token;
}

test('multi-character operators are complete, single scoped tokens', () => {
  const operators = {
    '>=': 'comparison', '<=': 'comparison', '==': 'comparison', '!=': 'comparison',
    '<-': 'state', '->': 'connection', '=>': 'choice',
    '<<': 'shift', '>>': 'shift', '<=>': 'equivalence', '@': 'annotation',
    '&&': 'logical', '||': 'logical', '..': 'range',
  };
  for (const [operator, category] of Object.entries(operators)) {
    const { lines } = tokenize(`left ${operator} right`);
    const token = expectScope(lines[0], operator, `keyword.operator.${category}.zlang`);
    assert.equal(token.text, operator);
    assert.deepEqual(token.scopes, ['source.zlang', `keyword.operator.${category}.zlang`]);
  }
});

test('nested type arguments and compile-time calls retain their own scopes', () => {
  const { lines } = tokenize('out payload : vec<index_width(8),vec<2,bits<8>>> = values');
  expectScope(lines[0], 'payload', 'variable.parameter.port.zlang');
  expectScope(lines[0], 'index_width', 'support.function.builtin.compile-time.zlang');
  expectScope(lines[0], 'bits', 'support.type.scalar.zlang');
  expectScope(lines[0], 'values', 'variable.other.readwrite.zlang');
  const angles = lines[0].tokens.filter((token) => token.text === '<' || token.text === '>');
  assert.equal(angles.length, 6);
  for (const token of angles) {
    const direction = token.text === '<' ? 'begin' : 'end';
    assert.ok(token.scopes.includes(`punctuation.definition.type.${direction}.zlang`));
    assert.ok(!token.scopes.some((scope) => scope.startsWith('keyword.operator.')));
  }
  assert.equal(expectScope(lines[0], 'bits', 'meta.type.vector.zlang').scopes.filter(
    (scope) => scope === 'meta.type.vector.zlang',
  ).length, 2);

  for (const expression of [
    'bits<index_width(8)>', 'fixed<index_width(8),1>',
    'string<index_width(8)>', 'fifo<u8,index_width(8)>',
    'credit<u8,index_width(8)>', 'Wrapper<index_width(8)>',
    'zeros<index_width(8)>', 'repeat<index_width(8)>(value)',
    'truncate<index_width(8)>(value)', 'identity<index_width(8)>(value)',
  ]) {
    const line = tokenize(`value = ${expression}`).lines[0];
    expectScope(line, 'index_width', 'support.function.builtin.compile-time.zlang');
  }
});

test('multiline generic declarations preserve types, defaults, and the next module', () => {
  const { lines } = tokenize([
    'module Generic<type T,',
    '    image : vec<4,u8>, IW=index_width(4), operation : fn(u8) -> u9>',
    '{',
    '    out result : bits<IW>',
    '}',
    'module Next {',
  ].join('\n'));
  expectScope(lines[0], 'Generic', 'entity.name.type.module.zlang');
  expectScope(lines[0], 'T', 'entity.name.type.parameter.zlang');
  expectScope(lines[1], 'vec', 'support.type.vector.zlang');
  expectScope(lines[1], 'u8', 'support.type.scalar.zlang');
  expectScope(lines[1], 'index_width', 'support.function.builtin.compile-time.zlang');
  expectScope(lines[1], 'fn', 'storage.type.function.zlang');
  expectScope(lines[1], '->', 'keyword.operator.connection.zlang');
  expectScope(lines[3], 'result', 'variable.parameter.port.zlang');
  const next = expectScope(lines[5], 'Next', 'entity.name.type.module.zlang');
  assert.ok(!next.scopes.includes('meta.declaration.parameters.zlang'));
});

test('user, hardware, conversion, compile-time, and guard calls retain style categories', () => {
  const { lines } = tokenize([
    'fn widen(x : u8) { extend<9>(x) }',
    'result = widen(input) + repeat<2>(input)',
    'other = bitcast<bits<8>>(input)',
    'size = index_width(4)',
    'equiv identity { x | zero<x> <=> x when unsigned(x) && width(x) == 8 }',
    'nested = bitcast<vec<2,bits<8>>>(input)',
    'copied = identity<vec<2,vec<2,u8>>>(input)',
  ].join('\n'));
  expectScope(lines[0], 'widen', 'entity.name.function.zlang');
  expectScope(lines[0], 'extend', 'support.function.builtin.conversion.zlang');
  expectScope(lines[1], 'widen', 'entity.name.function.call.zlang');
  expectScope(lines[1], 'repeat', 'support.function.builtin.hardware.zlang');
  expectScope(lines[2], 'bitcast', 'support.function.builtin.conversion.zlang');
  expectScope(lines[3], 'index_width', 'support.function.builtin.compile-time.zlang');
  expectScope(lines[4], 'unsigned', 'support.function.guard.zlang');
  expectScope(lines[4], 'width', 'support.function.guard.zlang');
  expectScope(lines[5], 'bitcast', 'support.function.builtin.conversion.zlang');
  expectScope(lines[6], 'identity', 'entity.name.function.call.zlang');
  for (const line of lines.slice(5)) {
    assert.ok(line.tokens.filter((token) => token.text === '<' || token.text === '>').every(
      (token) => token.scopes.some((scope) => scope.startsWith('punctuation.definition.type.')),
    ));
  }
});

test('declarations and references stay distinct without reserving contextual names', () => {
  const { lines } = tokenize([
    'module Signals {',
    '    in assert, /* contextual ports */ require : bit',
    '    reg held : bit = 0',
    '    local : bit = assert',
    '    result = held && local && require',
    '    assert safety @clk { result }',
    '    contract behavior {',
    '        require input_valid { assert }',
    '    }',
    '}',
  ].join('\n'));
  expectScope(lines[0], 'Signals', 'entity.name.type.module.zlang');
  expectScope(lines[1], 'assert', 'variable.parameter.port.zlang');
  expectScope(lines[1], 'require', 'variable.parameter.port.zlang');
  expectScope(lines[1], 'contextual ports', 'comment.block.zlang');
  expectScope(lines[2], 'held', 'variable.other.definition.hardware.zlang');
  expectScope(lines[3], 'local', 'variable.other.definition.zlang');
  expectScope(lines[3], 'assert', 'variable.other.readwrite.zlang');
  for (const symbol of ['held', 'local', 'require']) {
    expectScope(lines[4], symbol, 'variable.other.readwrite.zlang');
  }
  expectScope(lines[5], 'assert', 'keyword.control.verification.zlang');
  expectScope(lines[5], 'safety', 'entity.name.function.property.zlang');
  expectScope(lines[6], 'behavior', 'entity.name.type.verification-scope.zlang');
  expectScope(lines[7], 'require', 'keyword.control.verification.zlang');
  expectScope(lines[7], 'assert', 'variable.other.readwrite.zlang');
});

test('enable is a directive only within a resource, not a CSR field', () => {
  const { lines } = tokenize([
    'resource Device {',
    '    enable signal',
    '}',
    'csr control {',
    '    CONTROL @0x00 {',
    '        enable bit @0 rw = 0',
    '    }',
    '}',
    'result = enable',
  ].join('\n'));
  expectScope(lines[1], 'enable', 'keyword.other.directive.target.zlang');
  expectScope(lines[5], 'enable', 'variable.other.definition.csr.zlang');
  expectScope(lines[8], 'enable', 'variable.other.readwrite.zlang');
});

test('strings and character escapes do not become comments or leak past an unfinished line', () => {
  const { lines } = tokenize([
    String.raw`out text : string<8> = "// /* \\"`,
    String.raw`out quote : char = '\''`,
    String.raw`out byte : char = '\x41'`,
    String.raw`out invalid : char = '\q'`,
    'out unfinished : string<2> = "x',
    'module Recovered {',
  ].join('\n'));
  expectScope(lines[0], '// /* ', 'string.quoted.double.zlang');
  expectScope(lines[0], String.raw`\\`, 'constant.character.escape.zlang');
  assert.ok(lines[0].tokens.every((token) => !token.scopes.some((scope) => scope.startsWith('comment.'))));
  expectScope(lines[1], String.raw`'\''`, 'constant.character.zlang');
  expectScope(lines[2], String.raw`'\x41'`, 'constant.character.zlang');
  expectScope(lines[3], String.raw`\q`, 'invalid.illegal.escape.zlang');
  expectScope(lines[5], 'Recovered', 'entity.name.type.module.zlang');

  const escaped = tokenize(String.raw`out quote : string<3> = "a\"b" // actual comment`).lines[0];
  expectScope(escaped, String.raw`\"`, 'constant.character.escape.zlang');
  expectScope(escaped, 'actual comment', 'comment.line.double-slash.zlang');
  const invalid = tokenize(String.raw`out invalid : string<2> = "\q"`).lines[0];
  expectScope(invalid, String.raw`\q`, 'invalid.illegal.escape.zlang');
});

test('block comments and unfinished type arguments carry and release multiline state', () => {
  const { lines } = tokenize([
    '/* unfinished module Fake {',
    '"string" // still inside comment',
    '*/ module Real {',
    '    out payload : vec<',
    '        2, bits<8>>',
    '    result = payload',
  ].join('\n'));
  expectScope(lines[0], 'module Fake', 'comment.block.zlang');
  expectScope(lines[1], 'string', 'comment.block.zlang');
  expectScope(lines[2], 'Real', 'entity.name.type.module.zlang');
  expectScope(lines[4], 'bits', 'meta.type.vector.zlang');
  const result = expectScope(lines[5], 'result', 'variable.other.readwrite.zlang');
  assert.deepEqual(result.scopes, ['source.zlang', 'variable.other.readwrite.zlang']);
});

test('reasonable nested editor input completes within the tokenizer budget', () => {
  // Exercise recursive lookahead success and failure on ~1–4 KB editor lines.
  // Use TextMate's generous work budget, not a machine-speed microbenchmark.
  for (const depth of [10, 20]) {
    for (const size of [1024, 4096]) {
      const prefix = `value = identity<${'vec<2,'.repeat(depth)}`;
      const padding = 'width_parameter + '.repeat(Math.ceil(size / 18)).slice(0, size - prefix.length);
      const closing = '>'.repeat(depth + 1);
      for (const suffix of ['', closing, `${closing}(payload)`]) {
        const source = `${prefix}${padding}${suffix}`;
        const result = grammar.tokenizeLine(source, textmate.INITIAL, 1000);
        assert.equal(result.stoppedEarly, false, `Tokenizer stopped at depth ${depth}, size ${source.length}`);
        assert.ok(result.tokens.length > 0);
      }
    }
  }
});

test('the existing executable language tour tokenizes deterministically with balanced state', () => {
  const corpusPath = path.resolve(extension, '../../../examples/all_syntax.zhl');
  const source = fs.readFileSync(corpusPath, 'utf8');
  const first = tokenize(source);
  const second = tokenize(source);
  assert.deepEqual(first.lines, second.lines);
  assert.ok(first.stack.equals(second.stack));
  for (const line of first.lines) {
    assert.ok(line.tokens.every((token) => !token.scopes.some(
      (scope) => scope.startsWith('invalid.'),
    )), `Valid corpus contains an invalid lexical scope: ${line.text}`);
  }
  // Append a sentinel to detect a declaration, comment, or string scope that
  // leaked through the real corpus rather than checking regex text in isolation.
  const sentinel = tokenize(`${source}\nmodule TokenizationSentinel {`).lines.at(-1);
  const name = expectScope(sentinel, 'TokenizationSentinel', 'entity.name.type.module.zlang');
  assert.deepEqual(name.scopes, ['source.zlang', 'entity.name.type.module.zlang']);
  const line = first.lines.find((item) => item.text.includes('IW=index_width(N)'));
  assert.ok(line);
  expectScope(line, 'index_width', 'support.function.builtin.compile-time.zlang');
});
