/*
 * joule:code – helper tool for Java based on JavaParser.
 *
 * Reads a JSON request from stdin, writes a JSON answer to stdout
 * (same schema as tool.php/tool.js, see joulecode/lang/tooling.py):
 *   functions       {source}                 methods/constructors with spans, signature, Javadoc, complexity
 *   parse_function  {code, method, owner}    checks that `code` is exactly one method/constructor
 *   lint            {source}                 syntax check
 *   canonical       {source}                 token sequence without whitespace/comments
 *   file_info       {source}                 MI, SLOC, imports, packages, declared types
 *   analyze         {root, files}            functions + file_info for many files
 *
 * Qualified names: `Class.method`, nested `Outer.Inner.method`; overloaded
 * methods additionally get the (simplified) parameter types: `Class.method(String,int)`.
 * No JSON package needed – input and output are implemented by hand below.
 */

import com.github.javaparser.JavaParser;
import com.github.javaparser.JavaToken;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.Problem;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.comments.Comment;
import com.github.javaparser.ast.comments.JavadocComment;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.ConditionalExpr;
import com.github.javaparser.ast.stmt.CatchClause;
import com.github.javaparser.ast.stmt.DoStmt;
import com.github.javaparser.ast.stmt.ForEachStmt;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.ast.stmt.SwitchEntry;
import com.github.javaparser.ast.stmt.WhileStmt;
import com.github.javaparser.ast.type.Type;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Collectors;

public final class Tool {

    private static final List<String> JDK_PREFIXES =
            List.of("java.", "javax.", "jdk.", "sun.", "com.sun.", "org.w3c.dom", "org.xml.sax", "org.ietf.jgss");

    // -- Parsing -------------------------------------------------------------------------
    static CompilationUnit parse(String source) {
        ParserConfiguration config = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE);
        ParseResult<CompilationUnit> result = new JavaParser(config).parse(source);
        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            Problem p = result.getProblems().isEmpty() ? null : result.getProblems().get(0);
            String where = p == null ? "" : p.getLocation()
                    .flatMap(l -> l.getBegin().getRange()).map(r -> " in line " + r.begin.line).orElse("");
            throw new IllegalArgumentException("Syntax error: "
                    + (p == null ? "unknown" : p.getMessage().split("\n")[0]) + where);
        }
        return result.getResult().get();
    }

    static int line(Optional<com.github.javaparser.Range> r, boolean begin) {
        return r.map(x -> begin ? x.begin.line : x.end.line).orElse(0);
    }

    // -- Complexity --------------------------------------------------------------------------
    /** Cyclomatic complexity as PMD CyclomaticComplexity: 1 + branches + && / ||. */
    static int complexity(Node body) {
        if (body == null) {
            return 1;
        }
        int[] cc = {1};
        body.walk(n -> {
            if (n instanceof IfStmt || n instanceof ForStmt || n instanceof ForEachStmt || n instanceof WhileStmt
                    || n instanceof DoStmt || n instanceof CatchClause || n instanceof ConditionalExpr) {
                cc[0]++;
            } else if (n instanceof SwitchEntry && !((SwitchEntry) n).getLabels().isEmpty()) {
                cc[0] += ((SwitchEntry) n).getLabels().size();
            } else if (n instanceof BinaryExpr) {
                BinaryExpr.Operator op = ((BinaryExpr) n).getOperator();
                if (op == BinaryExpr.Operator.AND || op == BinaryExpr.Operator.OR) {
                    cc[0]++;
                }
            }
        });
        return cc[0];
    }

    // -- Signature and Javadoc ---------------------------------------------------------------
    /** Simple, erased type name: `java.util.Map.Entry<K,V>` → `Entry`, `int[]` stays. */
    static String simpleType(Type t) {
        String s = t.asString().replaceAll("<.*>", "");
        String arrays = s.replaceAll("^[^\\[]*", "");
        String base = s.replace(arrays, "");
        return base.substring(base.lastIndexOf('.') + 1) + arrays;
    }

    static String paramKey(CallableDeclaration<?> c) {
        return c.getParameters().stream()
                .map(p -> simpleType(p.getType()) + (p.isVarArgs() ? "..." : ""))
                .collect(Collectors.joining(","));
    }

    static String signature(CallableDeclaration<?> c) {
        List<String> params = new ArrayList<>();
        for (Parameter p : c.getParameters()) {
            params.add(p.getType().asString().replaceAll("\\s+", "") + (p.isVarArgs() ? "..." : "")
                    + " " + p.getNameAsString() + (p.isFinal() ? "[final]" : ""));
        }
        String ret = c instanceof MethodDeclaration
                ? ((MethodDeclaration) c).getType().asString().replaceAll("\\s+", "") : "";
        String mods = c.getModifiers().stream().map(m -> m.getKeyword().asString()).sorted()
                .collect(Collectors.joining(","));
        String tparams = c.getTypeParameters().stream().map(Node::toString).collect(Collectors.joining(","));
        String throwsList = c.getThrownExceptions().stream().map(t -> t.asString()).sorted()
                .collect(Collectors.joining(","));
        return "<" + tparams + ">" + ret + " " + c.getNameAsString() + "(" + String.join(",", params) + ")"
                + "throws(" + throwsList + ")[" + mods + "]";
    }

    static String docText(String raw) {
        String body = raw.replaceAll("^/\\*\\*", "").replaceAll("\\*/$", "");
        return body.lines().map(l -> l.replaceAll("^\\s*\\*( ?)", "")).collect(Collectors.joining("\n")).strip();
    }

    // -- Functions ----------------------------------------------------------------------------
    static Map<String, Object> info(CallableDeclaration<?> c, String qualname) {
        Map<String, Object> m = new LinkedHashMap<>();
        Optional<Comment> comment = c.getComment();
        JavadocComment doc = comment.filter(x -> x instanceof JavadocComment).map(x -> (JavadocComment) x).orElse(null);
        int nodeStart = line(c.getRange(), true);
        int docStart = doc == null ? 0 : line(doc.getRange(), true);
        Node body = c instanceof MethodDeclaration
                ? ((MethodDeclaration) c).getBody().orElse(null) : ((ConstructorDeclaration) c).getBody();
        m.put("qualname", qualname);
        m.put("start", doc == null ? nodeStart : Math.min(docStart, nodeStart));
        m.put("end", line(c.getRange(), false));
        m.put("node_start", nodeStart);
        m.put("def_line", line(c.getName().getRange(), true));
        m.put("doc_start", docStart);
        m.put("doc_end", doc == null ? 0 : line(doc.getRange(), false));
        m.put("signature", signature(c));
        String text = doc == null ? null : docText("/**" + doc.getContent() + "*/");
        m.put("docstring", text == null || text.isEmpty() ? null : text);
        m.put("complexity", complexity(body));
        m.put("abstract", body == null);
        return m;
    }

    static void collect(TypeDeclaration<?> type, String prefix, List<Map<String, Object>> out) {
        String owner = prefix + type.getNameAsString();
        List<CallableDeclaration<?>> callables = new ArrayList<>();
        for (BodyDeclaration<?> member : type.getMembers()) {
            if (member instanceof MethodDeclaration || member instanceof ConstructorDeclaration) {
                callables.add((CallableDeclaration<?>) member);
            }
        }
        Map<String, Integer> count = new HashMap<>();
        for (CallableDeclaration<?> c : callables) {
            count.merge(label(c), 1, Integer::sum);
        }
        for (CallableDeclaration<?> c : callables) {
            String name = label(c);
            String q = owner + "." + name + (count.get(name) > 1 ? "(" + paramKey(c) + ")" : "");
            out.add(info(c, q));
        }
        for (BodyDeclaration<?> member : type.getMembers()) {
            if (member instanceof TypeDeclaration) {
                collect((TypeDeclaration<?>) member, owner + ".", out);
            }
        }
    }

    static String label(CallableDeclaration<?> c) {
        return c instanceof ConstructorDeclaration ? "<init>" : c.getNameAsString();
    }

    static List<Map<String, Object>> functions(CompilationUnit cu) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (TypeDeclaration<?> t : cu.getTypes()) {
            collect(t, "", out);
        }
        return out;
    }

    // -- File metrics ------------------------------------------------------------------------------
    static double[] tokenMetrics(CompilationUnit cu) {
        Set<Integer> code = new TreeSet<>();
        Set<Integer> comments = new TreeSet<>();
        Set<String> operators = new LinkedHashSet<>();
        Set<String> operands = new LinkedHashSet<>();
        int n1 = 0;
        int n2 = 0;
        for (JavaToken tok : tokens(cu)) {
            JavaToken.Category cat = tok.getCategory();
            int b = line(tok.getRange(), true);
            int e = line(tok.getRange(), false);
            if (cat.isWhitespace()) {
                continue;
            }
            Set<Integer> target = cat.isComment() ? comments : code;
            for (int l = b; l <= e; l++) {
                target.add(l);
            }
            if (cat.isComment()) {
                continue;
            }
            if (cat.isIdentifier() || cat.isLiteral()) {
                operands.add(tok.getText());
                n2++;
            } else {
                operators.add(tok.getText());
                n1++;
            }
        }
        int vocabulary = operators.size() + operands.size();
        double volume = vocabulary > 0 ? (n1 + n2) * (Math.log(vocabulary) / Math.log(2)) : 0;
        comments.removeAll(code);
        return new double[] {code.size(), comments.size(), volume};
    }

    static List<JavaToken> tokens(CompilationUnit cu) {
        List<JavaToken> out = new ArrayList<>();
        cu.getTokenRange().ifPresent(range -> range.forEach(out::add));
        return out;
    }

    /** Maintainability index as in radon (variant with comment share), 0–100 – same formula as for the other languages. */
    static double mi(double volume, int cc, double sloc, double comments) {
        if (volume <= 0 || cc <= 0 || sloc <= 0) {
            return 100.0;
        }
        double pct = comments * 100.0 / sloc;
        double raw = 171 - 5.2 * Math.log(volume) - 0.23 * cc - 16.2 * Math.log(sloc)
                + 50 * Math.sin(Math.sqrt(2.46 * Math.toRadians(pct)));
        return Math.round(Math.min(Math.max(0, raw * 100 / 171), 100) * 100) / 100.0;
    }

    static boolean isJdk(String pkg) {
        return JDK_PREFIXES.stream().anyMatch(p -> pkg.equals(p.replaceAll("\\.$", "")) || pkg.startsWith(p));
    }

    static Map<String, Object> fileInfo(CompilationUnit cu, List<Map<String, Object>> fns) {
        List<String> uses = new ArrayList<>();
        int insertAfter = cu.getPackageDeclaration().map(p -> line(p.getRange(), false)).orElse(0);
        Set<String> modules = new TreeSet<>();
        List<String> referenced = new ArrayList<>();
        for (ImportDeclaration imp : cu.getImports()) {
            String text = imp.toString().strip().replaceAll("\\s+", " ");
            uses.add(text);
            insertAfter = Math.max(insertAfter, line(imp.getRange(), false));
            String name = imp.getNameAsString();
            // package of the import: for `import a.b.C;` → a.b, for `import a.b.*;` → a.b, for static → package of the class
            String pkg = imp.isAsterisk() && !imp.isStatic() ? name : name.contains(".") ? name.substring(0, name.lastIndexOf('.')) : name;
            if (imp.isStatic() && !imp.isAsterisk() && pkg.contains(".")) {
                pkg = pkg.substring(0, pkg.lastIndexOf('.'));
            }
            modules.add(pkg);
            if (!imp.isAsterisk()) {
                referenced.add(imp.isStatic() ? name.substring(0, name.lastIndexOf('.')) : name);
            }
        }
        String pkg = cu.getPackageDeclaration().map(p -> p.getNameAsString()).orElse("");
        List<String> declared = new ArrayList<>();
        cu.findAll(TypeDeclaration.class).forEach(t -> {
            Optional<String> fq = ((TypeDeclaration<?>) t).getFullyQualifiedName();
            fq.ifPresent(declared::add);
        });
        double[] tm = tokenMetrics(cu);
        int cc = fns.stream().mapToInt(f -> (Integer) f.get("complexity")).sum();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("uses", uses);
        m.put("use_insert_after", insertAfter);
        m.put("modules", new ArrayList<>(modules));
        m.put("jdk_modules", modules.stream().filter(Tool::isJdk).collect(Collectors.toList()));
        m.put("package", pkg);
        m.put("declared", declared);
        m.put("referenced", referenced);
        m.put("sloc", (int) tm[0]);
        m.put("mi", mi(tm[2], Math.max(cc, 1), tm[0], tm[1]));
        return m;
    }

    // -- Operations -------------------------------------------------------------------------------
    static Map<String, Object> parseFunction(String code, String owner) {
        String cls = owner == null || owner.isEmpty() ? "joule:codeWrapper" : owner;
        CompilationUnit cu = parse("class " + cls + " {\n" + code + "\n}\n");
        if (cu.getTypes().size() != 1 || !(cu.getType(0) instanceof ClassOrInterfaceDeclaration)) {
            throw new IllegalArgumentException("Expected exactly one method without further code.");
        }
        List<BodyDeclaration<?>> members = cu.getType(0).getMembers();
        if (members.size() != 1 || !(members.get(0) instanceof CallableDeclaration)) {
            throw new IllegalArgumentException("Expected exactly one method or constructor without further class members.");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", label((CallableDeclaration<?>) members.get(0)));
        return m;
    }

    static Map<String, Object> handle(Map<String, Object> req) throws IOException {
        String op = (String) req.get("op");
        Map<String, Object> out = new LinkedHashMap<>();
        switch (op) {
            case "functions":
                out.put("functions", functions(parse((String) req.get("source"))));
                return out;
            case "lint":
                parse((String) req.get("source"));
                return out;
            case "canonical": {
                String joined = tokens(parse((String) req.get("source"))).stream()
                        .filter(t -> !t.getCategory().isWhitespaceOrComment())
                        .map(JavaToken::getText).collect(Collectors.joining("\u0000"));
                out.put("canonical", joined);
                return out;
            }
            case "parse_function":
                return parseFunction((String) req.get("code"), (String) req.get("owner"));
            case "file_info": {
                CompilationUnit cu = parse((String) req.get("source"));
                return fileInfo(cu, functions(cu));
            }
            case "analyze": {
                Map<String, Object> files = new LinkedHashMap<>();
                Path root = Path.of((String) req.get("root"));
                for (Object rel : (List<?>) req.get("files")) {
                    String source = Files.readString(root.resolve((String) rel), StandardCharsets.UTF_8);
                    Map<String, Object> entry = new LinkedHashMap<>();
                    try {
                        CompilationUnit cu = parse(source);
                        List<Map<String, Object>> fns = functions(cu);
                        entry.put("functions", fns);
                        entry.put("info", fileInfo(cu, fns));
                    } catch (RuntimeException e) {
                        entry.put("error", e.getMessage());
                    }
                    files.put((String) rel, entry);
                }
                out.put("files", files);
                return out;
            }
            default:
                throw new IllegalArgumentException("Unknown operation: " + op);
        }
    }

    public static void main(String[] args) throws IOException {
        PrintStream stdout = new PrintStream(System.out, true, StandardCharsets.UTF_8);
        Map<String, Object> res = new LinkedHashMap<>();
        try {
            @SuppressWarnings("unchecked")
            Map<String, Object> req = (Map<String, Object>) new Json(readAll(System.in)).value();
            res.put("ok", true);
            res.putAll(handle(req));
        } catch (RuntimeException e) {
            res.clear();
            res.put("ok", false);
            res.put("error", String.valueOf(e.getMessage()));
        }
        stdout.print(Json.write(res));
    }

    static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        in.transferTo(buf);
        return buf.toString(StandardCharsets.UTF_8);
    }

    // -- Minimal JSON reader/writer ------------------------------------------------------------------
    static final class Json {
        private final String s;
        private int i;

        Json(String s) {
            this.s = s;
        }

        Object value() {
            ws();
            char c = s.charAt(i);
            if (c == '{') {
                Map<String, Object> m = new LinkedHashMap<>();
                i++;
                ws();
                if (s.charAt(i) == '}') {
                    i++;
                    return m;
                }
                while (true) {
                    ws();
                    String k = string();
                    ws();
                    i++; // :
                    m.put(k, value());
                    ws();
                    if (s.charAt(i++) == '}') {
                        return m;
                    }
                }
            }
            if (c == '[') {
                List<Object> l = new ArrayList<>();
                i++;
                ws();
                if (s.charAt(i) == ']') {
                    i++;
                    return l;
                }
                while (true) {
                    l.add(value());
                    ws();
                    if (s.charAt(i++) == ']') {
                        return l;
                    }
                }
            }
            if (c == '"') {
                return string();
            }
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
            return Double.parseDouble(s.substring(start, i));
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        private String string() {
            StringBuilder b = new StringBuilder();
            i++; // "
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') {
                    return b.toString();
                }
                if (c != '\\') {
                    b.append(c);
                    continue;
                }
                char e = s.charAt(i++);
                switch (e) {
                    case 'n': b.append('\n'); break;
                    case 't': b.append('\t'); break;
                    case 'r': b.append('\r'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                    default: b.append(e);
                }
            }
        }

        static String write(Object o) {
            StringBuilder b = new StringBuilder();
            write(o, b);
            return b.toString();
        }

        @SuppressWarnings("unchecked")
        private static void write(Object o, StringBuilder b) {
            if (o == null) {
                b.append("null");
            } else if (o instanceof String) {
                b.append('"');
                for (char c : ((String) o).toCharArray()) {
                    switch (c) {
                        case '"': b.append("\\\""); break;
                        case '\\': b.append("\\\\"); break;
                        case '\n': b.append("\\n"); break;
                        case '\r': b.append("\\r"); break;
                        case '\t': b.append("\\t"); break;
                        default:
                            if (c < 0x20) {
                                b.append(String.format("\\u%04x", (int) c));
                            } else {
                                b.append(c);
                            }
                    }
                }
                b.append('"');
            } else if (o instanceof Map) {
                b.append('{');
                boolean first = true;
                for (Map.Entry<String, Object> e : ((Map<String, Object>) o).entrySet()) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    write(e.getKey(), b);
                    b.append(':');
                    write(e.getValue(), b);
                }
                b.append('}');
            } else if (o instanceof List) {
                b.append('[');
                boolean first = true;
                for (Object x : (List<Object>) o) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    write(x, b);
                }
                b.append(']');
            } else {
                b.append(o);
            }
        }
    }
}
