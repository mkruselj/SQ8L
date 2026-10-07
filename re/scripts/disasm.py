"""Quick disassembler: dis.py ADDR [count]"""
import sys, pefile, capstone, os
ROOT=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pe=pefile.PE(os.path.join(ROOT,"original","SQ8L.dll")); IB=pe.OPTIONAL_HEADER.ImageBase
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
def dis(addr,n=40,stop_ret=True):
    code=pe.get_data(addr-IB,n*8)
    out=[]
    for i,ins in enumerate(md.disasm(code,addr)):
        out.append(f"  {ins.address:#x}: {ins.mnemonic} {ins.op_str}")
        if i>=n-1 or (stop_ret and ins.mnemonic in("ret",) ): break
    return "\n".join(out)
if __name__=="__main__":
    a=int(sys.argv[1],16); n=int(sys.argv[2]) if len(sys.argv)>2 else 40
    print(dis(a,n))
