// Compiles every *.json schema in a folder to a .ts file of the same name with
// json-schema-to-typescript, the library behind the json2ts CLI, in ONE node
// process: the CLI's output, in seconds instead of a process per file.
// AppCreator.generateTypeScriptSchemas runs it.
//
//   node server/tools/json2tsBatch.js <schema json dir> <ts output dir>
//
// The library is the globally installed one (`npm install -g
// json-schema-to-typescript`), the same install the json2ts CLI runs, at the
// major version below: another major may render the types differently, so it
// fails instead. Every schema is compiled first and the .ts files are written
// only when all of them succeed, so a failure leaves the folder as it was.
'use strict';

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const PACKAGE = 'json-schema-to-typescript';
const EXPECTED_MAJOR = 15;
const INSTALL = 'npm install -g ' + PACKAGE + '@' + EXPECTED_MAJOR;

function loadCompiler() {
  const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
  const packageDir = path.join(globalRoot, PACKAGE);
  let version;
  try {
    version = JSON.parse(fs.readFileSync(path.join(packageDir, 'package.json'), 'utf8')).version;
  } catch (error) {
    throw new Error(PACKAGE + ' is not installed globally under ' + globalRoot + ': ' + INSTALL);
  }
  if (Number(version.split('.')[0]) !== EXPECTED_MAJOR) {
    throw new Error('the global ' + PACKAGE + ' is ' + version + ', but the committed schemas were generated with ' +
      EXPECTED_MAJOR + '.x: ' + INSTALL);
  }
  return require(packageDir);
}

async function main() {
  const [inputDir, outputDir] = process.argv.slice(2);
  if (!inputDir || !outputDir) {
    throw new Error('usage: node json2tsBatch.js <schema json dir> <ts output dir>');
  }
  const { compileFromFile } = loadCompiler();
  const names = fs.readdirSync(inputDir).filter((name) => name.endsWith('.json')).sort();
  const compiled = [];
  for (const name of names) {
    try {
      compiled.push([name, await compileFromFile(path.join(inputDir, name))]);
    } catch (error) {
      throw new Error('could not compile ' + name + ': ' + error.message);
    }
  }
  fs.mkdirSync(outputDir, { recursive: true });
  for (const [name, source] of compiled) {
    const outputPath = path.join(outputDir, name.replace(/\.json$/, '.ts'));
    fs.writeFileSync(outputPath, source);
    console.log('Generated TypeScript type for ' + name + ' at ' + outputPath);
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
