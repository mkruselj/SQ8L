// Apply recovered Delphi names and export decompiled C per unit range.
// Args: <names.txt> <ranges.txt> <outDir>
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Reference;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;
import java.util.Set;
import java.util.TreeSet;

public class ExportUnits extends GhidraScript {
    // True if the bytes before a are a ret/jmp optionally followed by Delphi padding (8B C0, 90, 8D 40 00).
    private boolean followsReturn(Address a) throws Exception {
        long o = a.getOffset();
        for (int back = 0; back <= 3; back++) {
            Address p = toAddr(o - back - 1);
            int b = getByte(p) & 0xff;
            boolean padOk = true;
            for (long q = o - back; q < o; q++) {
                int pb = getByte(toAddr(q)) & 0xff;
                if (pb != 0x90 && pb != 0x8b && pb != 0xc0 && pb != 0x8d && pb != 0x40 && pb != 0x00) padOk = false;
            }
            if (!padOk) continue;
            if (b == 0xc3) return true;
            if (back + 3 <= 3 && (getByte(toAddr(o - back - 3)) & 0xff) == 0xc2) return true;
            Instruction ins = getInstructionContaining(p);
            if (ins != null && (ins.getFlowType().isTerminal() || (ins.getFlowType().isJump() && !ins.getFlowType().isConditional()))
                    && ins.getMaxAddress().equals(p)) return true;
        }
        return false;
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File namesFile = new File(args[0]);
        File rangesFile = new File(args[1]);
        File outDir = new File(args[2]);
        outDir.mkdirs();

        try (BufferedReader r = new BufferedReader(new FileReader(namesFile))) {
            String line;
            while ((line = r.readLine()) != null) {
                String[] p = line.trim().split("\\s+");
                if (p.length < 3) continue;
                Address a = toAddr(Long.parseLong(p[0].substring(2), 16));
                if (p[1].equals("F")) {
                    Function f = getFunctionAt(a);
                    if (f == null) {
                        disassemble(a);
                        f = createFunction(a, p[2]);
                    }
                    if (f != null) f.setName(p[2], SourceType.USER_DEFINED);
                } else {
                    createLabel(a, p[2], true, SourceType.USER_DEFINED);
                }
            }
        }

        // Make sure every range is fully disassembled into functions.
        try (BufferedReader r = new BufferedReader(new FileReader(rangesFile))) {
            String line;
            while ((line = r.readLine()) != null) {
                String[] p = line.trim().split("\\s+");
                Address s = toAddr(Long.parseLong(p[0].substring(2), 16));
                Address e = toAddr(Long.parseLong(p[1].substring(2), 16) - 1);
                Address a = s;
                while (a.compareTo(e) < 0) {
                    if (getInstructionAt(a) == null && getFunctionContaining(a) == null
                            && (a.getOffset() & 3) == 0 && followsReturn(a)) {
                        disassemble(a);
                        createFunction(a, null);
                    }
                    Function f = getFunctionContaining(a);
                    a = (f != null) ? f.getBody().getMaxAddress().add(1) : a.add(1);
                }
            }
        }

        DecompInterface ifc = new DecompInterface();
        DecompileOptions opts = new DecompileOptions();
        ifc.setOptions(opts);
        ifc.openProgram(currentProgram);

        try (BufferedReader r = new BufferedReader(new FileReader(rangesFile))) {
            String line;
            while ((line = r.readLine()) != null) {
                String[] p = line.trim().split("\\s+");
                Address s = toAddr(Long.parseLong(p[0].substring(2), 16));
                Address e = toAddr(Long.parseLong(p[1].substring(2), 16) - 1);
                String unit = p[2];
                try (PrintWriter w = new PrintWriter(new File(outDir, unit + ".c"));
                     PrintWriter asm = new PrintWriter(new File(outDir, unit + ".asm"))) {
                    w.println("// unit " + unit + " " + s + " - " + e);
                    FunctionIterator it = currentProgram.getFunctionManager().getFunctions(new AddressSet(s, e), true);
                    int n = 0;
                    for (Function f : it) {
                        n++;
                        Set<String> callers = new TreeSet<>();
                        for (Reference ref : getReferencesTo(f.getEntryPoint())) {
                            Function c = getFunctionContaining(ref.getFromAddress());
                            callers.add(c != null ? c.getName() : ref.getFromAddress().toString());
                        }
                        w.println();
                        w.println("// ==== " + f.getName() + " @ " + f.getEntryPoint() + "  size=" + f.getBody().getNumAddresses());
                        w.println("// callers: " + String.join(", ", callers));
                        asm.println();
                        asm.println("; ==== " + f.getName() + " @ " + f.getEntryPoint());
                        InstructionIterator ii = currentProgram.getListing().getInstructions(f.getBody(), true);
                        for (Instruction ins : ii) {
                            String lbl = getSymbolAt(ins.getAddress()) != null && !ins.getAddress().equals(f.getEntryPoint())
                                    ? getSymbolAt(ins.getAddress()).getName() + ":\n" : "";
                            StringBuilder ann = new StringBuilder();
                            for (Reference ref : ins.getReferencesFrom()) {
                                if (ref.getReferenceType().isCall()) {
                                    Function t = getFunctionAt(ref.getToAddress());
                                    if (t != null) ann.append(" ; -> ").append(t.getName());
                                }
                            }
                            asm.println(lbl + "  " + ins.getAddress() + "  " + ins + ann);
                        }
                        DecompileResults res = ifc.decompileFunction(f, 60, monitor);
                        if (res != null && res.decompileCompleted()) {
                            w.println(res.getDecompiledFunction().getC());
                        } else {
                            w.println("// decompilation failed: " + (res != null ? res.getErrorMessage() : "null"));
                        }
                    }
                    println(unit + ": " + n + " functions");
                }
            }
        }
        ifc.dispose();
    }
}
