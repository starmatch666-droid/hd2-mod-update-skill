-- HD2-Addon: mods/starm/owner_scout
-- READ-ONLY locator scout. No WriteProcessMemory, no VirtualProtect, no game calls.
--
-- v0.2.0: every locator the addons use is a pointer stored in game.dll's
-- writable data at a fixed RVA, and the update moved them all.  This walks
-- those sections and tests every candidate pointer against both fingerprints
-- the addons already carry, then reports the new RVAs.
local key = 'StarmOwnerScout'
if rawget(_G, key) then return rawget(_G, key) end
local M = { status = 'starting', done = false, version = '0.2.0' }
rawset(_G, key, M)

local loader = rawget(_G, 'CowboyBingusModLoader')
local previous_update = rawget(_G, 'update')
if type(loader) ~= 'table' or loader.api ~= 1 or type(loader.open_log) ~= 'function'
  or type(previous_update) ~= 'function' then
  M.status = 'unsupported_loader_or_update'
  return M
end

local ffi = require('ffi')
ffi.cdef [[
  void *GetModuleHandleA(const char *);
  void *GetCurrentProcess(void);
  int ReadProcessMemory(void *, const void *, void *, size_t, size_t *);
  size_t VirtualQuery(const void *, void *, size_t);
  uint64_t GetTickCount64(void);
  typedef struct _MEMORY_BASIC_INFORMATION {
    void *BaseAddress; void *AllocationBase; uint32_t AllocationProtect;
    uint16_t PartitionId; size_t RegionSize; uint32_t State; uint32_t Protect; uint32_t Type;
  } MEMORY_BASIC_INFORMATION;
]]
local kernel = ffi.load('kernel32')
local process = kernel.GetCurrentProcess()
local log = assert(loader.open_log('OwnerScout.log'), 'log_unavailable')
local LF = string.char(10)

-- 6.3: byte strings only ever come from hex, never from a table of numbers.
local function unhex(s)
  return (s:gsub('..', function(x) return string.char(tonumber(x, 16)) end))
end

INSERT_DIRECT
INSERT_SIGNATURES
INSERT_SLOTS

local OLD_RVA = 0x276F0C0
local CHUNK = 262144
local POLL_SECONDS = 2
local SEARCH_AFTER = 45
local STEP_BUDGET = 0.025
local MAX_CANDIDATES = 3000000

local function emit(s)
  local line = os.date('!%Y-%m-%dT%H:%M:%SZ') .. ' ' .. s
  pcall(function() log:write(line .. LF); log:flush() end)
  M.status = s
end
local function now() return tonumber(kernel.GetTickCount64()) / 1000 end
local function u32at(s, i)
  local a, b, c, d = s:byte(i, i + 3)
  if not d then return nil end
  return a + b * 256 + c * 65536 + d * 16777216
end
local function u64at(s, i)
  local lo, hi = u32at(s, i), u32at(s, i + 4)
  if not lo or not hi then return nil end
  if hi >= 0x00100000 then return nil end
  return lo + hi * 4294967296
end
local function read(address, size)
  if type(address) ~= 'number' or address < 65536 or size < 1 or size > CHUNK then return nil end
  local out, count = ffi.new('uint8_t[?]', size), ffi.new('size_t[1]')
  local ok = pcall(function()
    if kernel.ReadProcessMemory(process, ffi.cast('const void *', address), out, size, count) == 0
      or tonumber(count[0]) ~= size then error('short_read') end
  end)
  if not ok then return nil end
  return ffi.string(out, size)
end
local mbi = ffi.new('MEMORY_BASIC_INFORMATION[1]')
local function region_of(address)
  if type(address) ~= 'number' or address < 65536 then return nil end
  local n = tonumber(kernel.VirtualQuery(ffi.cast('const void *', address), mbi, ffi.sizeof(mbi[0])))
  if n == 0 then return nil end
  local base = tonumber(ffi.cast('uintptr_t', mbi[0].BaseAddress))
  local size = tonumber(mbi[0].RegionSize)
  if not base or not size or size < 1 then return nil end
  if mbi[0].State ~= 0x1000 then return nil end
  local prot = tonumber(mbi[0].Protect) % 256
  if not (prot == 2 or prot == 4 or prot == 8 or prot == 32 or prot == 64 or prot == 128) then return nil end
  return base, size
end
local function table_matches(address)
  if not address or address < 65536 then return nil end
  for i = 1, #signatures do
    local s = signatures[i]
    if read(address + s.offset, #s.bytes) == s.bytes then return s.name end
  end
  return nil
end

-- family 1: a record whose neighbouring records still match the addons' guards
local head_hits, guard_fails = 0, 0
local function check_direct(v)
  local head = read(v, 8)
  if not head then return nil end
  for i = 1, #direct do
    local d = direct[i]
    if head == d.head then
      head_hits = head_hits + 1
      local ok = true
      for j = 1, #d.guards do
        local g = d.guards[j]
        if read(v + g.off, #g.bytes) ~= g.bytes then ok = false; break end
      end
      if ok and d.size > 0 and read(v, d.size) ~= d.original then ok = false end
      if ok then return d.label, d.old_rva end
      guard_fails = guard_fails + 1
    end
  end
  return nil
end

-- family 2: a pointer to a component table, found at one of the known slots
local function check_owner(v)
  for i = 1, #owner_slots do
    local s = owner_slots[i]
    local raw = read(v + s, 8)
    local p = raw and u64at(raw, 1)
    if p and p >= 65536 then
      local name = table_matches(p)
      if name then return s, p, name end
    end
  end
  return nil
end

-- ---------------------------------------------------------------- phase 1
local base
pcall(function()
  local h = kernel.GetModuleHandleA('game.dll')
  if h ~= nil then base = tonumber(ffi.cast('uintptr_t', h)) end
end)
local old_slot = base and (base + OLD_RVA) or nil
local started_at = now()
local last_poll, last_raw, old_owner, old_ok = 0, nil, nil, false
local found_any = false
emit(string.format('scout_start base=0x%X old_rva=0x%X direct=%d maps=%d slots=%d',
  base or 0, OLD_RVA, #direct, #signatures, #owner_slots))

local function raw8hex(address)
  local s = read(address, 8)
  if not s then return 'READFAIL' end
  local t = {}
  for i = 1, 8 do t[i] = string.format('%02x', s:byte(i)) end
  return table.concat(t)
end
local function poll()
  if not old_slot then return end
  local t = now()
  if t - last_poll < POLL_SECONDS then return end
  last_poll = t
  local hex = raw8hex(old_slot)
  if hex ~= last_raw then
    last_raw = hex
    emit(string.format('scout_old_rva_raw %s at=%.1fs', hex, t - started_at))
  end
  if not old_owner then
    local s = read(old_slot, 8)
    local p = s and u64at(s, 1)
    if p and p >= 65536 then
      old_owner = p
      local off, target, name
      for i = 1, #owner_slots do
        local q = u64at(read(p + owner_slots[i], 8) or '', 1)
        if q then
          local nm = table_matches(q)
          if nm then off, target, name = owner_slots[i], q, nm; break end
        end
      end
      if name then
        old_ok = true
        emit(string.format('scout_old_rva_valid owner=0x%X slot=0x%X table=0x%X map=%s',
          p, off, target, name))
      else
        emit(string.format('scout_old_rva_nonzero_but_no_table owner=0x%X', p))
      end
    end
  end
end

-- ---------------------------------------------------------------- phase 2
local sections, section_i, offset = {}, 1, 0
local function parse_sections()
  if not base then return false end
  local hdr = read(base, 4096)
  if not hdr then return false end
  local e = u32at(hdr, 0x3D)
  if not e or e < 1 or e > 4096 - 64 then return false end
  if hdr:sub(e + 1, e + 4) ~= 'PE\0\0' then return false end
  local nsec = hdr:byte(e + 7) + hdr:byte(e + 8) * 256
  local optsz = hdr:byte(e + 21) + hdr:byte(e + 22) * 256
  if nsec < 1 or nsec > 64 or e + 24 + optsz + nsec * 40 > #hdr then return false end
  for i = 0, nsec - 1 do
    local o = e + 24 + optsz + i * 40 + 1
    local vsize, va, ch = u32at(hdr, o + 8), u32at(hdr, o + 12), u32at(hdr, o + 36)
    if vsize and va and ch and va > 0 and vsize > 0 and ch >= 0x80000000 then
      sections[#sections + 1] = { va = va, size = vsize }
      emit(string.format('scout_section rva=0x%X size=0x%X', va, vsize))
    end
  end
  return #sections > 0
end

local candidates, owner_checked, direct_seen = 0, 0, 0
local function search_iterate(budget)
  local deadline = now() + budget
  while section_i <= #sections and now() < deadline do
    local s = sections[section_i]
    if offset >= s.size then
      section_i = section_i + 1; offset = 0
      if section_i > #sections then break end
    else
      local n = math.min(CHUNK, s.size - offset)
      local buf = read(base + s.va + offset, n)
      if buf then
        for i = 1, #buf - 7, 8 do
          local v = u64at(buf, i)
          if v and v >= 0x10000 then
            candidates = candidates + 1
            if candidates > MAX_CANDIDATES then break end
            local where = base + s.va + offset + i - 1
            local label, old_rva = check_direct(v)
            if label then
              found_any = true; direct_seen = direct_seen + 1
              emit(string.format('scout_FOUND_DIRECT new_rva=0x%X old_rva=0x%X ptr=0x%X component=%s',
                where - base, old_rva, v, label))
            end
            local rb, rsz = region_of(v)
            if rb and rsz >= 0x1000000 then
              owner_checked = owner_checked + 1
              local slot, target, name = check_owner(v)
              if name then
                found_any = true
                emit(string.format(
                  'scout_FOUND_OWNER new_owner_rva=0x%X owner=0x%X slot=0x%X table=0x%X map=%s',
                  where - base, v, slot, target, name))
              end
            end
          end
        end
      end
      offset = offset + n
    end
  end
  return section_i > #sections
end

local search_started = false
local function step()
  if M.done then return end
  poll()
  if not search_started then
    if (old_ok or (now() - started_at) >= SEARCH_AFTER) and parse_sections() then
      search_started = true
      emit(string.format('scout_search_start sections=%d waited=%.1fs', #sections, now() - started_at))
    end
    return
  end
  local ok, done = pcall(search_iterate, STEP_BUDGET)
  if not ok then
    emit('scout_search_error ' .. tostring(done)); M.done = true; return
  end
  if done then
    M.done = true
    emit(string.format(
      'scout_finish found=%s candidates=%d head_hits=%d guard_fails=%d owner_checked=%d direct_found=%d elapsed=%.1fs',
      tostring(found_any), candidates, head_hits, guard_fails, owner_checked, direct_seen, now() - started_at))
    pcall(function() log:close() end)
  end
end

local elapsed = 0
update = function(dt, ...)
  if type(dt) == 'number' and dt >= 0 and dt < math.huge then elapsed = elapsed + dt end
  if elapsed >= 0.1 then elapsed = 0; pcall(step) end
  return previous_update(dt, ...)
end
local previous_shutdown = rawget(_G, 'shutdown')
shutdown = function(...)
  if not M.done then
    emit(string.format('scout_finish reason=shutdown found=%s candidates=%d head_hits=%d guard_fails=%d searched=%s',
      tostring(found_any), candidates, head_hits, guard_fails, tostring(search_started)))
    pcall(function() log:close() end)
  end
  if previous_shutdown then return previous_shutdown(...) end
end
return M
