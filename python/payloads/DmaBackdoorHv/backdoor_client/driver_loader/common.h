
#define KUSER_SHARED_DATA 0xfffff78000000000

#define RVATOVA(_base_, _offset_) ((PUCHAR)(_base_) + (ULONG)(_offset_))

#define _ALIGN_DOWN(x, align)(x &~ (align - 1))
#define _ALIGN_UP(x, align)((x & (align - 1)) ? _ALIGN_DOWN(x, align) + align : x)


#define IFMT32 "0x%.8x"
#define IFMT64 "0x%.16I64x"

#define IFMT32_W L"0x%.8x"
#define IFMT64_W L"0x%.16I64x"

#ifdef _X86_

#define IFMT IFMT32
#define IFMT_W IFMT32_W

#elif _AMD64_

#define IFMT IFMT64
#define IFMT_W IFMT64_W

#endif
