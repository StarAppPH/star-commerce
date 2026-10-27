/**
 * Unused-response-field analyzer: which fields of each route's response does
 * no client code read by name? server/test_unused_response_fields.py runs it,
 * applies the exclusions and renders the brief; run that test to see the result.
 *
 *   npx tsx scripts/unusedResponseFields.ts --schemas <dir> --route-models <file>
 *       [--skip <path>]... [--root <client dir>]
 *
 * Paths are relative to the client root (default: this script's parent). The
 * caller passes them from AppCreator's constants: --schemas is the generated
 * schema folder, --route-models the generated file declaring RouteResponseModels
 * and the generated request methods, and each --skip a file or folder (ending in
 * /) whose reads do not count: generated files and tooling. It prints one JSON
 * object (AnalyzerResult).
 *
 * For every route in RouteResponseModels it walks the response type tree
 * (arrays as `[]`, index signatures as `{}`, intersections by their parts) and
 * gives each property path a status:
 *   used        TypeScript find-references reaches the property from client code:
 *               on the schema type itself, on a same-named duplicate declaration
 *               in another schema file, or on any type the value flows into
 *               (assignment, argument, setState, return, JSX prop, `as` cast, a
 *               spread into a typed value or into an object literal; nested
 *               properties pair by name; a library or generated wrapper such
 *               as `Promise<ApiResponse<T>>` argument by argument, so a route
 *               behind a function-typed prop or config counts; a subtype that
 *               TypeScript drops from a union (`a ? b : c`, `a ?? b`, `a || b`,
 *               an array literal, a call through a union of route methods such
 *               as `serverRequests[fnName]`) into the kept members it is
 *               assignable to); or the value is sent back to the server (an
 *               argument of a generated request method, or a value declared
 *               with a RouteRequestBodies type), which reads every field it gets;
 *   usedByName  no typed reference, but the name is read some other way: as a
 *               string literal (a DataGrid `field`, `obj[key]` keys), on an untyped
 *               value, or via a client-declared type no detected flow reaches;
 *   dynamic     the type, or a type it flows into, meets a violation below;
 *   unused      none of the above: the server can stop sending it.
 *
 * Violations (strict dynamic access), where x's type is a response schema type
 * or a client type a response type flows into; read the fields by name instead:
 *   - `Object.keys/entries/values/getOwnPropertyNames/getOwnPropertyDescriptors(x)`,
 *     `Reflect.ownKeys(x)` and `for (const k in x)`;
 *   - `JSON.stringify(x)` (except a request body being sent);
 *   - a rest element, `const { a, ...rest } = x`;
 *   - x flowing anywhere loose: a spread, argument, assignment, return, property,
 *     JSX prop or `as` cast into any, unknown, object, {}, a type parameter or an
 *     index signature (unless x is that same type). React's dependency arrays
 *     only compare identity and are not loose, and an untyped `let v;` takes
 *     the type of what is assigned to it;
 *   - x passed to a parameter of a client-declared generic whose type mentions a
 *     type parameter T anywhere (`value?: T`, `T | null`, `ReadonlyArray<T>`,
 *     `Record<string, T>`, `{ data: T }`) when the body feeds any value whose
 *     type mentions T into one of the above (the parameter, `w.data`, an element,
 *     a for-of or callback variable, a copy), or passes it on to another such
 *     generic. A generic that only hands T back is fine.
 *     A library's generic (useState, map) is judged by its instantiated type, so
 *     a library that serializes its T (firebase setDoc/addDoc, algolia) is not
 *     caught: an accepted trade-off.
 * Mapping over arrays, named reads, destructuring without a rest element and a
 * spread into a typed value (`{...order, status}`) are fine.
 *
 * Anything the analyzer could not type is a problem in the result, never skipped.
 */
import path from 'node:path';
import { Node, Project, SyntaxKind, type Symbol as MorphSymbol, type Type } from 'ts-morph';

type PathStatus = 'used' | 'usedByName' | 'dynamic' | 'unused';

interface PathResult {
  status: PathStatus;
  iface: string;
  evidence: string;
}

interface RouteResult {
  responseType: string;
  paths: Record<string, PathResult>;
  // Unused paths whose parent path is not itself unused (what to remove).
  unused: string[];
}

interface Violation {
  where: string;
  kind: string;
  types: string[];
  code: string;
}

interface AnalyzerResult {
  routes: Record<string, RouteResult>;
  violations: Violation[];
  // What the analyzer could not judge: each one fails the gate.
  problems: string[];
  stats: { clientFiles: number; routes: number; seconds: number };
}

interface Options {
  root: string;
  schemas: string;
  routeModels: string;
  skip: string[];
}

function parseArgs(argv: string[]): Options {
  let root = path.resolve(__dirname, '..');
  let schemas = '';
  let routeModels = '';
  const skip: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const value = argv[i + 1];
    if (argv[i] === '--root' && value) root = path.resolve(value);
    else if (argv[i] === '--schemas' && value) schemas = value;
    else if (argv[i] === '--route-models' && value) routeModels = value;
    else if (argv[i] === '--skip' && value) skip.push(value);
    else throw new Error(`Unknown argument: ${argv[i]} (see the usage at the top of this script)`);
    i++;
  }
  if (!schemas || !routeModels) throw new Error('--schemas and --route-models are required');
  return { root, schemas, routeModels, skip };
}

const norm = (p: string): string => p.replace(/\\/g, '/');
// Library calls that only test or convert a value, never read its fields.
const SAFE_CALLEES = /^(Array\.isArray|Boolean|String|Number|isNaN|Number\.isNaN|Number\.isFinite|console\.\w+)$/;
const MAX_PROBLEMS = 40;
// Calls that enumerate a value's own keys.
const ENUMERATORS = /^(Object\.(keys|values|entries|getOwnPropertyNames|getOwnPropertyDescriptors)|Reflect\.ownKeys)$/;

function analyze(options: Options): AnalyzerResult {
  const started = Date.now();
  const rootNorm = norm(options.root);
  const under = (rel: string): string => `${rootNorm}/${norm(rel).replace(/^\.\//, '')}`;
  const schemaDir = `${under(options.schemas).replace(/\/$/, '')}/`;
  const routeModelsFile = under(options.routeModels);
  const skipped = options.skip.map(under);
  const project = new Project({ tsConfigFilePath: path.join(options.root, 'tsconfig.json') });
  const checker = project.getTypeChecker();
  // Route problems are never capped; untypable nodes are, with their total.
  const routeProblems: string[] = [];
  const nodeProblems: string[] = [];
  let swallowed = 0;

  const rel = (file: string): string => norm(path.relative(options.root, file));
  const fileOf = (node: Node): string => norm(node.getSourceFile().getFilePath());
  const isSchemaFile = (file: string): boolean => file.startsWith(schemaDir);
  // Not client code: outside the root, dependencies, build output and --skip paths.
  const isExcludedFile = (file: string): boolean =>
    !file.startsWith(`${rootNorm}/`) ||
    /\/(node_modules|\.next)\//.test(file) ||
    isSchemaFile(file) ||
    skipped.some((s) => (s.endsWith('/') ? file.startsWith(s) : file === s));

  const clientFiles = project.getSourceFiles().filter((sf) => !isExcludedFile(norm(sf.getFilePath())));

  // A schema type's key: its interface or alias name, or for an anonymous type
  // literal its file and position (they would all be `__type` otherwise).
  const literalKey = (decl: Node): string => `${path.basename(fileOf(decl))}@${decl.getStart()}`;
  function schemaKeyOf(t: Type): string | null {
    const alias = t.getAliasSymbol();
    const aliasDecl = alias?.getDeclarations()[0];
    if (alias && aliasDecl && isSchemaFile(fileOf(aliasDecl))) return alias.getName();
    const sym = t.getSymbol();
    const decl = sym?.getDeclarations()[0];
    if (!sym || !decl || !isSchemaFile(fileOf(decl))) return null;
    return sym.getName().startsWith('__') ? literalKey(decl) : sym.getName();
  }

  // A union's or intersection's member types, else the type itself.
  const partsOf = (t: Type): Type[] => (t.isUnion() ? t.getUnionTypes() : t.isIntersection() ? t.getIntersectionTypes() : [t]);

  function propType(t: Type, name: string): Type | null {
    const p = t.getProperty(name);
    if (!p) return null;
    const d = p.getValueDeclaration() ?? p.getDeclarations()[0];
    return d ? checker.getTypeOfSymbolAtLocation(p, d) : null;
  }

  // Descends a response type: unions, intersections, arrays (`[]`), index
  // signatures (`{}`) and schema types, once per type on the current path.
  // Calls onProperty for every schema type property; its return value is the
  // context handed to that property's own descent.
  function descend<C>(
    t: Type,
    prefix: string,
    stack: string[],
    ctx: C,
    onProperty: (iface: string, prop: MorphSymbol, pathText: string, ctx: C) => C,
  ): void {
    if (t.isUnion()) return t.getUnionTypes().forEach((u) => descend(u, prefix, stack, ctx, onProperty));
    if (t.isIntersection()) return t.getIntersectionTypes().forEach((u) => descend(u, prefix, stack, ctx, onProperty));
    if (t.isArray()) return descend(t.getArrayElementType() as Type, `${prefix}[]`, stack, ctx, onProperty);
    const iface = schemaKeyOf(t);
    if (!iface) {
      const idx = t.isObject() ? t.getStringIndexType() : undefined;
      if (idx) descend(idx, `${prefix}{}`, stack, ctx, onProperty);
      return;
    }
    if (stack.includes(iface)) return;
    const idx = t.getStringIndexType();
    if (idx) descend(idx, `${prefix}{}`, [...stack, iface], ctx, onProperty);
    for (const p of t.getProperties()) {
      const decl = p.getDeclarations()[0];
      if (!decl) continue;
      const pathText = prefix ? `${prefix}.${p.getName()}` : p.getName();
      const childCtx = onProperty(iface, p, pathText, ctx);
      descend(checker.getTypeOfSymbolAtLocation(p, decl), pathText, [...stack, iface], childCtx, onProperty);
    }
  }

  // ---- Object-type keys. Schema types are keyed by schemaKeyOf, so duplicate
  // declarations across schema files unify; client-declared object types by
  // file, name and position.
  const keyTypes = new Map<string, Type[]>();
  function objKeysOfType(t: Type | undefined, out = new Set<string>(), depth = 0): Set<string> {
    if (!t || depth > 5) return out;
    if (t.isUnion()) {
      t.getUnionTypes().forEach((u) => objKeysOfType(u, out, depth));
      return out;
    }
    if (t.isIntersection()) {
      t.getIntersectionTypes().forEach((u) => objKeysOfType(u, out, depth));
      return out;
    }
    if (t.isArray()) return objKeysOfType(t.getArrayElementType(), out, depth + 1);
    let key = schemaKeyOf(t);
    // A client alias of a library mapped type (`type Row = Pick<Order, 'a'>`):
    // its symbol is the library's, so key it by the alias; its properties keep
    // the picked interface's declarations, so their references still count.
    const alias = t.getAliasSymbol();
    const aliasDecl = alias?.getDeclarations()[0];
    if (!key && alias && aliasDecl && !isExcludedFile(fileOf(aliasDecl)) && t.isObject() &&
        !t.getCallSignatures().length && /\/node_modules\//.test(fileOf(t.getSymbol()?.getDeclarations()[0] ?? aliasDecl))) {
      key = `USER:${rel(fileOf(aliasDecl))}:${alias.getName()}@${aliasDecl.getStart()}`;
    }
    if (!key) {
      const sym = t.getSymbol() ?? t.getAliasSymbol();
      const decl = sym?.getDeclarations()[0];
      if (!sym || !decl) return out;
      const file = fileOf(decl);
      if (!isExcludedFile(file) && t.isObject() && !t.getCallSignatures().length) {
        key = `USER:${rel(file)}:${sym.getName()}@${decl.getStart()}`;
      } else if (isExcludedFile(file)) {
        // Library and generated wrappers (Promise, SetStateAction, ReadonlyArray,
        // Partial, ApiResponse...): their type arguments.
        t.getTypeArguments().forEach((a) => objKeysOfType(a, out, depth + 1));
        t.getAliasTypeArguments().forEach((a) => objKeysOfType(a, out, depth + 1));
      }
    }
    if (key) {
      out.add(key);
      const known = keyTypes.get(key) ?? [];
      if (known.length < 3 && !known.includes(t)) known.push(t);
      keyTypes.set(key, known);
    }
    return out;
  }

  // Keys of a value serialized or sent whole: through arrays, unions and the
  // properties of anonymous or client-declared wrapper objects.
  function objKeysDeep(t: Type | undefined, out = new Set<string>(), depth = 0, seen = new Set<Type>()): Set<string> {
    if (!t || depth > 4 || seen.has(t)) return out;
    seen.add(t);
    objKeysOfType(t).forEach((k) => out.add(k));
    for (const part of partsOf(t)) {
      if (part.isArray()) {
        objKeysDeep(part.getArrayElementType(), out, depth + 1, seen);
        continue;
      }
      if (!part.isObject() || part.getCallSignatures().length || schemaKeyOf(part)) continue;
      for (const p of part.getProperties()) objKeysDeep(propType(part, p.getName()) ?? undefined, out, depth + 1, seen);
    }
    return out;
  }

  // A declaration typed with RouteRequestBodies (serverFetch's `body`): a value
  // being sent to the server.
  function declaresRequestBody(decl: Node | undefined): boolean {
    const typeNode =
      decl && (Node.isParameterDeclaration(decl) || Node.isVariableDeclaration(decl)) ? decl.getTypeNode() : undefined;
    return /\bRouteRequestBodies\b/.test(typeNode?.getText() ?? '');
  }
  const isRequestBody = (node: Node): boolean =>
    Node.isIdentifier(node) && declaresRequestBody(node.getSymbol()?.getValueDeclaration());

  // `typeParams: false` when a type parameter hands T on rather than loses it.
  function isLoose(t: Type | undefined, typeParams = true): boolean {
    if (!t) return false;
    if (t.isUnion() || t.isIntersection()) {
      return partsOf(t).some((u) => !u.isNull() && !u.isUndefined() && isLoose(u, typeParams));
    }
    if (t.isTypeParameter()) return typeParams;
    if (t.isAny() || t.isUnknown()) return true;
    const text = t.getText();
    if (text === 'object' || text === '{}' || text === 'Object') return true;
    const idx = t.getStringIndexType();
    return Boolean(idx && (idx.isAny() || idx.isUnknown()));
  }
  const hasNamedFields = (t: Type): boolean =>
    partsOf(t).some((u) => u.isObject() && !u.isArray() && !u.getCallSignatures().length && u.getProperties().length > 0);
  // React's dependency arrays only compare identity.
  const isDependencyList = (t: Type | undefined): boolean => t?.getAliasSymbol()?.getName() === 'DependencyList';

  // Whether `from` loses its field types going into `to`: a loose target, or a
  // named-field value becoming the entries of an index signature. The same
  // type on both sides never is.
  function losesFields(from: Type, to: Type | undefined): boolean {
    if (!to || isDependencyList(to)) return false;
    if (from.isArray() && to.isArray()) return losesFields(from.getArrayElementTypeOrThrow(), to.getArrayElementTypeOrThrow());
    const toKeys = objKeysOfType(to);
    if ([...objKeysOfType(from)].some((k) => toKeys.has(k))) return false;
    if (isLoose(to)) return true;
    return partsOf(to).some((u) => Boolean(u.getStringIndexType())) && hasNamedFields(from);
  }

  // ---- Flow edges: a value of key A flows into a value of key B.
  const edges = new Map<string, Set<string>>();
  function addEdge(a: string, b: string): boolean {
    if (a === b) return false;
    const targets = edges.get(a) ?? new Set<string>();
    edges.set(a, targets);
    if (targets.has(b)) return false;
    targets.add(b);
    return true;
  }
  // A library or generated wrapper's name and type arguments, else null.
  function wrapperArgs(t: Type): { name: string; args: Type[] } | null {
    const alias = t.getAliasSymbol();
    const aliasDecl = alias?.getDeclarations()[0];
    if (alias && aliasDecl && isExcludedFile(fileOf(aliasDecl)) && t.getAliasTypeArguments().length) {
      return { name: alias.getFullyQualifiedName(), args: t.getAliasTypeArguments() };
    }
    const sym = t.getSymbol();
    const decl = sym?.getDeclarations()[0];
    if (sym && decl && isExcludedFile(fileOf(decl)) && !schemaKeyOf(t) && t.getTypeArguments().length) {
      return { name: sym.getFullyQualifiedName(), args: t.getTypeArguments() };
    }
    return null;
  }
  function addFlow(fromT: Type | undefined, toT: Type | undefined): void {
    if (!fromT || !toT) return;
    // The same wrapper on both sides (Promise<ApiResponse<A>> into
    // Promise<ApiResponse<B>>): its type arguments pair by position.
    const fromWrapper = wrapperArgs(fromT);
    const toWrapper = wrapperArgs(toT);
    if (fromWrapper && toWrapper && fromWrapper.name === toWrapper.name && fromWrapper.args.length === toWrapper.args.length) {
      fromWrapper.args.forEach((arg, i) => addFlow(arg, toWrapper.args[i]));
      return;
    }
    const to = objKeysOfType(toT);
    for (const a of objKeysOfType(fromT)) for (const b of to) addEdge(a, b);
  }

  // TypeScript drops a union member that is a subtype of another (`a ? b : c`,
  // `a ?? b`, `a || b`, an array literal, a call through a union of functions
  // such as `serverRequests[fnName]`), so reads on the result reference only
  // the survivors: each operand type missing from the result flows into the
  // kept members it is assignable to (the ones that absorbed it), never into an
  // unrelated member that merely shares the union.
  // (`a && b` yields only a's falsy part, never its object type.)
  function operandTypes(node: Node): Type[] {
    if (Node.isConditionalExpression(node)) return [node.getWhenTrue().getType(), node.getWhenFalse().getType()];
    if (Node.isBinaryExpression(node)) {
      const op = node.getOperatorToken().getKind();
      if (op === SyntaxKind.QuestionQuestionToken || op === SyntaxKind.BarBarToken) {
        return [node.getLeft().getType(), node.getRight().getType()];
      }
      return op === SyntaxKind.AmpersandAmpersandToken ? [node.getRight().getType()] : [];
    }
    // A React dependency list's elements are only compared, never read.
    if (Node.isArrayLiteralExpression(node)) {
      return isDependencyList(node.getContextualType()) ? [] : node.getElements().map((e) => e.getType());
    }
    if (Node.isCallExpression(node)) {
      const callee = node.getExpression().getType();
      return callee.isUnion() ? callee.getUnionTypes().flatMap((u) => u.getCallSignatures().map((s) => s.getReturnType())) : [];
    }
    return [];
  }
  const keyAssignable = (a: string, b: string): boolean =>
    (keyTypes.get(a) ?? []).some((ta) => (keyTypes.get(b) ?? []).some((tb) => ta.isAssignableTo(tb)));
  function flowReducedOperands(node: Node): void {
    const operands = operandTypes(node);
    if (!operands.length) return;
    const result = objKeysOfType(node.getType());
    for (const operand of operands) {
      for (const a of objKeysOfType(operand)) {
        // Precision guard; no fixture distinguishes it today.
        if (result.has(a)) continue;
        for (const b of result) if (keyAssignable(a, b)) addEdge(a, b);
      }
    }
  }

  // ---- Dynamic-access sites; whether each is a violation is decided once the
  // flow graph is known.
  interface DynSite {
    keys: Set<string>;
    deep: boolean;
    kind: string;
    where: string;
    code: string;
  }
  const dynSites: DynSite[] = [];
  const snippet = (node: Node): string => node.getText().replace(/\s+/g, ' ').slice(0, 120);
  const pushDyn = (keys: Set<string>, deep: boolean, kind: string, where: string, code: Node | string): void => {
    if (keys.size) dynSites.push({ keys, deep, kind, where, code: typeof code === 'string' ? code : snippet(code) });
  };
  // A value's own named-field keys: what Object.keys and for...in enumerate.
  // Over an array or a record's data keys it is no field read.
  const namedFieldKeys = (t: Type): Set<string> =>
    t.isArray() ? new Set() : new Set([...objKeysOfType(t)].filter((k) => (keyTypes.get(k) ?? []).some(hasNamedFields)));

  // Parameters of client-declared generics that reach a dynamic sink, and the
  // call sites passing values to a generic's type-parameter parameter (judged
  // once every body is seen). Judged per type parameter: when a sink receives a
  // value whose type mentions T (the parameter itself, `w.data`, an element, a
  // for-of or callback variable, a copy, ...), every parameter declared inside
  // T's owner (function, class, interface or type alias) whose type mentions T
  // is dynamic.
  const dynamicParams = new Set<Node>();
  interface GenericCall {
    keys: Set<string>;
    param: Node;
    argTypeParams: Set<Node>;
    kind: string;
    where: string;
    code: string;
  }
  const genericCalls: GenericCall[] = [];
  // The declarations of the type parameters a type mentions anywhere: through
  // unions (null and undefined included), arrays, type arguments, object
  // properties and index signatures.
  function typeParamsIn(t: Type, out = new Set<Node>(), depth = 0, seen = new Set<Type>()): Set<Node> {
    if (depth > 4 || seen.has(t)) return out;
    seen.add(t);
    if (t.isTypeParameter()) {
      const decl = t.getSymbol()?.getDeclarations()[0];
      if (decl) out.add(decl);
      return out;
    }
    [
      ...(t.isUnion() || t.isIntersection() ? partsOf(t) : []),
      ...(t.isArray() ? [t.getArrayElementTypeOrThrow()] : []),
      ...t.getTypeArguments(),
      ...t.getAliasTypeArguments(),
      ...(t.getStringIndexType() ? [t.getStringIndexType() as Type] : []),
    ].forEach((u) => typeParamsIn(u, out, depth + 1, seen));
    if (t.isObject() && !t.getCallSignatures().length && !schemaKeyOf(t)) {
      for (const p of t.getProperties()) {
        const pt = propType(t, p.getName());
        if (pt) typeParamsIn(pt, out, depth + 1, seen);
      }
    }
    return out;
  }
  // Marks every parameter declared inside each type parameter's client-declared
  // owner (a function, class, interface or type alias, nested closures and
  // methods included) whose type mentions it; whether any was new.
  function markTypeParams(typeParams: Set<Node>): boolean {
    let changed = false;
    for (const tp of typeParams) {
      const owner = tp.getParent();
      if (!owner || isExcludedFile(fileOf(owner))) continue;
      for (const param of owner.getDescendantsOfKind(SyntaxKind.Parameter)) {
        if (!dynamicParams.has(param) && typeParamsIn(param.getType()).has(tp)) {
          dynamicParams.add(param);
          changed = true;
        }
      }
    }
    return changed;
  }
  const noteGenericSink = (t: Type): void => {
    markTypeParams(typeParamsIn(t));
  };

  // Every flow of a value into a typed target: an edge, or a deep violation
  // when the target loses the value's field types.
  function flowInto(from: Type | undefined, to: Type | undefined, kind: string, where: string, code: Node): void {
    if (!from) return;
    // A generic's value into a loose target; a type-parameter target hands T
    // on instead (judged through genericCalls).
    if (to && isLoose(to, false) && !isDependencyList(to)) noteGenericSink(from);
    const keys = objKeysOfType(from);
    if (!keys.size) return;
    if (losesFields(from, to)) pushDyn(keys, true, kind, where, code);
    else addFlow(from, to);
  }

  // An untyped `let x;`: TypeScript gives it the type of each value assigned to
  // it, so an assignment is no flow into a loose value.
  function isEvolvingLet(target: Node): boolean {
    const decl = Node.isIdentifier(target) ? target.getSymbol()?.getValueDeclaration() : undefined;
    return Boolean(decl && Node.isVariableDeclaration(decl) && !decl.getTypeNode() && !decl.getInitializer());
  }

  // Values sent back to the server: key -> where.
  const echoed = new Map<string, string>();
  const markEchoed = (t: Type, where: string): void =>
    objKeysDeep(t).forEach((k) => {
      if (!echoed.has(k)) echoed.set(k, where);
    });

  // ---- Names read without a typed reference.
  const nameIdx = new Map<string, string>();
  const noteName = (name: string, how: string): void => {
    if (!nameIdx.has(name)) nameIdx.set(name, how);
  };
  function noteRead(name: string, decls: Node[], where: string): void {
    if (!decls.length) noteName(name, `read on an untyped value at ${where}`);
    else if (decls.some((d) => !isExcludedFile(fileOf(d)))) noteName(name, `read via a client-declared type at ${where}`);
  }

  function visitCall(node: Node, where: string): void {
    if (!Node.isCallExpression(node)) return;
    const callee = node.getExpression().getText();
    const args = node.getArguments();
    if (ENUMERATORS.test(callee) && args[0]) {
      pushDyn(namedFieldKeys(args[0].getType()), false, callee, where, node);
      noteGenericSink(args[0].getType());
      return;
    }
    if (callee === 'JSON.stringify' && args[0]) {
      // A request body being sent: its senders were recorded at their call sites.
      if (isRequestBody(args[0])) return;
      pushDyn(objKeysDeep(args[0].getType()), true, callee, where, node);
      noteGenericSink(args[0].getType());
      return;
    }
    if (SAFE_CALLEES.test(callee)) return;
    const decl = checker.getResolvedSignature(node)?.getDeclaration();
    const params = decl && Node.isParametered(decl) ? decl.getParameters() : [];
    const clientDeclared = decl ? !isExcludedFile(fileOf(decl)) : false;
    const sendsToServer = decl ? fileOf(decl) === routeModelsFile : false;
    args.forEach((a, i) => {
      if (!Node.isExpression(a)) return;
      const param = params[Math.min(i, params.length - 1)];
      if (sendsToServer || declaresRequestBody(param)) {
        markEchoed(a.getType(), where);
        addFlow(a.getType(), a.getContextualType());
        return;
      }
      const kind = `passed to a loose parameter of ${callee.slice(0, 40)}`;
      if (clientDeclared && param && typeParamsIn(param.getType()).size) {
        genericCalls.push({ keys: objKeysDeep(a.getType()), param, argTypeParams: typeParamsIn(a.getType()), kind, where, code: snippet(node) });
      }
      flowInto(a.getType(), a.getContextualType(), kind, where, node);
    });
  }

  for (const sf of clientFiles) {
    const file = rel(sf.getFilePath());
    sf.forEachDescendant((node) => {
      const where = `${file}:${node.getStartLineNumber()}`;
      try {
        if (Node.isSpreadAssignment(node) || Node.isJsxSpreadAttribute(node)) {
          // Into the contextual type, else the object literal's own type.
          const expr = node.getExpression();
          const target = expr.getContextualType() ?? node.getParentOrThrow().getType();
          flowInto(expr.getType(), target, 'spread into a loose value', where, node.getParentOrThrow());
        } else if (Node.isCallExpression(node)) {
          visitCall(node, where);
        } else if (Node.isBindingElement(node) && node.getDotDotDotToken() && Node.isObjectBindingPattern(node.getParent())) {
          pushDyn(objKeysOfType(node.getParentOrThrow().getType()), true, 'rest element', where, node.getParentOrThrow());
        } else if (Node.isForInStatement(node)) {
          pushDyn(namedFieldKeys(node.getExpression().getType()), false, 'for...in', where, node.getExpression());
          noteGenericSink(node.getExpression().getType());
        } else if (Node.isJsxAttribute(node)) {
          const init = node.getInitializer();
          const expr = init && Node.isJsxExpression(init) ? init.getExpression() : undefined;
          if (expr) flowInto(expr.getType(), expr.getContextualType(), 'passed to a loose prop', where, node);
        } else if (Node.isVariableDeclaration(node)) {
          const init = node.getInitializer();
          if (init && node.getTypeNode()) flowInto(init.getType(), node.getType(), 'assigned to a loose value', where, node);
        } else if (Node.isPropertyAssignment(node)) {
          const init = node.getInitializer();
          if (init) flowInto(init.getType(), init.getContextualType(), 'put in a loose property', where, node);
        } else if (Node.isShorthandPropertyAssignment(node)) {
          const name = node.getNameNode();
          flowInto(name.getType(), name.getContextualType(), 'put in a loose property', where, node);
        } else if (Node.isReturnStatement(node)) {
          const expr = node.getExpression();
          if (expr) flowInto(expr.getType(), expr.getContextualType(), 'returned as a loose value', where, node);
        } else if (Node.isArrowFunction(node)) {
          const body = node.getBody();
          if (Node.isExpression(body)) flowInto(body.getType(), body.getContextualType(), 'returned as a loose value', where, body);
        } else if (Node.isBinaryExpression(node) && node.getOperatorToken().getKind() === SyntaxKind.EqualsToken &&
                   !isEvolvingLet(node.getLeft())) {
          flowInto(node.getRight().getType(), node.getLeft().getType(), 'assigned to a loose value', where, node);
        } else if (Node.isAsExpression(node) || Node.isTypeAssertion(node) || Node.isSatisfiesExpression(node)) {
          flowInto(node.getExpression().getType(), node.getType(), 'cast to a loose type', where, node);
        } else if (Node.isArrayLiteralExpression(node) || Node.isConditionalExpression(node)) {
          flowInto(node.getType(), node.getContextualType(), 'put in a loose value', where, node);
        }

        flowReducedOperands(node);

        if (Node.isPropertyAccessExpression(node)) {
          const nameNode = node.getNameNode();
          noteRead(nameNode.getText(), nameNode.getSymbol()?.getDeclarations() ?? [], where);
        } else if (Node.isBindingElement(node) && Node.isObjectBindingPattern(node.getParent())) {
          const pn = node.getPropertyNameNode() ?? node.getNameNode();
          if (Node.isIdentifier(pn)) {
            const prop = node.getParentOrThrow().getType().getProperty(pn.getText());
            noteRead(pn.getText(), prop?.getDeclarations() ?? [], where);
          }
        } else if (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node)) {
          const value = node.getLiteralValue();
          if (/^[A-Za-z_$][\w$]*$/.test(value) && !Node.isImportDeclaration(node.getParent())) {
            noteName(value, `appears as a string literal at ${where}`);
          }
        }
      } catch (error) {
        swallowed++;
        if (nodeProblems.length < MAX_PROBLEMS) {
          nodeProblems.push(`could not type ${node.getKindName()} at ${where}: ${String(error).split('\n')[0]}`);
        }
      }
    });
  }

  // A generic's parameter is dynamic when its body feeds it to a dynamic
  // parameter of another generic, transitively.
  for (let changed = true; changed; ) {
    changed = false;
    for (const c of genericCalls) {
      if (dynamicParams.has(c.param)) changed = markTypeParams(c.argTypeParams) || changed;
    }
  }
  for (const c of genericCalls) {
    if (dynamicParams.has(c.param)) pushDyn(c.keys, true, c.kind, c.where, c.code);
  }

  // Nested pairing: when A flows into B, A.p's object types flow into B.p's.
  for (let round = 0, changed = true; changed && round < 6; round++) {
    changed = false;
    for (const [a, targets] of [...edges.entries()]) {
      for (const b of [...targets]) {
        for (const ta of keyTypes.get(a) ?? []) {
          for (const tb of keyTypes.get(b) ?? []) {
            for (const p of ta.getProperties()) {
              const pa = propType(ta, p.getName());
              const pb = propType(tb, p.getName());
              if (!pa || !pb) continue;
              for (const x of objKeysOfType(pa)) for (const y of objKeysOfType(pb)) changed = addEdge(x, y) || changed;
            }
          }
        }
      }
    }
  }
  const reachCache = new Map<string, Set<string>>();
  function reach(key: string): Set<string> {
    const cached = reachCache.get(key);
    if (cached) return cached;
    const seen = new Set([key]);
    const queue = [key];
    while (queue.length) {
      for (const y of edges.get(queue.shift() as string) ?? []) {
        if (!seen.has(y)) {
          seen.add(y);
          queue.push(y);
        }
      }
    }
    reachCache.set(key, seen);
    return seen;
  }

  // ---- Property references, keyed as schemaKeyOf keys them: interfaces,
  // aliased type literals and anonymous type literals.
  const schemaDecls = new Map<string, Node[]>();
  const addDecl = (key: string, member: Node, name: string): void => {
    const k = `${key}.${name}`;
    schemaDecls.set(k, [...(schemaDecls.get(k) ?? []), member]);
  };
  for (const sf of project.getSourceFiles().filter((s) => isSchemaFile(norm(s.getFilePath())))) {
    for (const iface of sf.getInterfaces()) for (const p of iface.getProperties()) addDecl(iface.getName(), p, p.getName());
    for (const literal of sf.getDescendantsOfKind(SyntaxKind.TypeLiteral)) {
      const parent = literal.getParent();
      const key = Node.isTypeAliasDeclaration(parent) ? parent.getName() : literalKey(literal);
      for (const p of literal.getProperties()) addDecl(key, p, p.getName());
    }
  }
  function declsFor(key: string, prop: string): Node[] {
    if (!key.startsWith('USER:')) return schemaDecls.get(`${key}.${prop}`) ?? [];
    return (keyTypes.get(key) ?? []).flatMap((t) => t.getProperty(prop)?.getDeclarations() ?? []);
  }
  const refCache = new Map<string, string[]>();
  function refsFor(key: string, prop: string): string[] {
    const cacheKey = `${key}.${prop}`;
    const cached = refCache.get(cacheKey);
    if (cached) return cached;
    const out = new Set<string>();
    for (const d of declsFor(key, prop)) {
      const nameNode = Node.hasName(d) ? d.getNameNode() : undefined;
      if (!nameNode || !Node.isReferenceFindable(nameNode)) continue;
      for (const r of nameNode.findReferencesAsNodes()) {
        if (r === nameNode || isExcludedFile(fileOf(r))) continue;
        out.add(`${rel(r.getSourceFile().getFilePath())}:${r.getStartLineNumber()}`);
        if (out.size >= 3) break;
      }
      if (out.size >= 3) break;
    }
    const refs = [...out];
    refCache.set(cacheKey, refs);
    return refs;
  }

  // ---- Walk each route's response type.
  const routeModels = project.getSourceFileOrThrow(routeModelsFile).getInterfaceOrThrow('RouteResponseModels');

  // Response schema types, and every key their values flow into.
  const responseKeys = new Set<string>();
  for (const m of routeModels.getProperties()) {
    descend(m.getType(), '', [], null, (iface) => {
      responseKeys.add(iface);
      return null;
    });
  }
  const responseFed = new Set<string>();
  responseKeys.forEach((k) => reach(k).forEach((r) => responseFed.add(r)));

  const violationSites = dynSites.filter((s) => [...s.keys].some((k) => responseFed.has(k)));
  const dynamicByKey = new Map<string, DynSite>();
  for (const s of violationSites) for (const k of s.keys) if (!dynamicByKey.has(k) || s.deep) dynamicByKey.set(k, s);
  const displayKey = (k: string): string => k.replace(/^USER:/, '').replace(/@\d+$/, '');

  // What a property's descendants inherit: a deep violation, or being sent
  // back to the server.
  interface Inherited {
    dyn: DynSite | null;
    echo: string | null;
  }
  function inheritedFor(iface: string, from: Inherited): Inherited {
    let { dyn, echo } = from;
    for (const k of reach(iface)) {
      const site = dynamicByKey.get(k);
      if (site && (!dyn || site.deep)) dyn = site;
      echo = echo ?? echoed.get(k) ?? null;
    }
    return { dyn, echo };
  }

  function classify(iface: string, name: string, inherited: Inherited): PathResult {
    const targets = reach(iface);
    for (const k of targets) {
      const refs = refsFor(k, name);
      if (refs.length) return { status: 'used', iface, evidence: refs[0] };
    }
    if (inherited.echo) return { status: 'used', iface, evidence: `sent back to the server at ${inherited.echo}` };
    const byName = nameIdx.get(name);
    if (byName) return { status: 'usedByName', iface, evidence: byName };
    const { dyn } = inherited;
    if (dyn) return { status: 'dynamic', iface, evidence: `${dyn.kind} at ${dyn.where}` };
    return { status: 'unused', iface, evidence: `no read of ${iface}.${name} or of the ${targets.size - 1} type(s) it flows into` };
  }

  const routes: Record<string, RouteResult> = {};
  for (const m of routeModels.getProperties()) {
    const paths: Record<string, PathResult> = {};
    descend<Inherited>(m.getType(), '', [], { dyn: null, echo: null }, (iface, prop, pathText, from) => {
      const here = inheritedFor(iface, from);
      paths[pathText] = classify(iface, prop.getName(), here);
      return { dyn: here.dyn && here.dyn.deep ? here.dyn : null, echo: here.echo };
    });
    const t = m.getType();
    if (!Object.keys(paths).length && !(schemaKeyOf(t) && t.getProperties().length === 0 && !t.getStringIndexType())) {
      routeProblems.push(`route ${m.getName()}: its response type ${t.getText()} yields no field paths (any, unresolved or not a schema type)`);
    }
    const parentOf = (p: string): string => p.replace(/(\[\]|\{\})*\.[^.]+$/, '').replace(/(\[\]|\{\})+$/, '');
    const hasUnusedAncestor = (p: string): boolean => {
      const parent = parentOf(p);
      return parent !== p && parent in paths && (paths[parent].status === 'unused' || hasUnusedAncestor(parent));
    };
    routes[m.getName()] = {
      responseType: m.getTypeNodeOrThrow().getText(),
      paths,
      unused: Object.keys(paths).filter((p) => paths[p].status === 'unused' && !hasUnusedAncestor(p)),
    };
  }

  return {
    routes,
    violations: violationSites.map((s) => ({
      where: s.where,
      kind: s.kind,
      types: [...s.keys].filter((k) => responseFed.has(k)).map(displayKey),
      code: s.code,
    })),
    problems: [...routeProblems, ...nodeProblems, ...(swallowed ? [`${swallowed} node(s) could not be typed in all`] : [])],
    stats: {
      clientFiles: clientFiles.length,
      routes: Object.keys(routes).length,
      seconds: Math.round((Date.now() - started) / 100) / 10,
    },
  };
}

process.stdout.write(`${JSON.stringify(analyze(parseArgs(process.argv.slice(2))))}\n`);
