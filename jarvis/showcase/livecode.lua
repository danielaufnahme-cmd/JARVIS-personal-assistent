-- JARVIS's live coding typist (section 25, the cinematic showcase): the init file of the Neovim the showcase starts
-- in its own terminal (`nvim --clean -n --cmd "let g:jarvis_livecode='…/livecode.json'" -u livecode.lua FILE`).
--
-- It is the typist itself, inside Neovim: no synthetic input, nothing is sent to any other window. It waits for the
-- runner's `go` file (the window is on screen), types the program into the buffer character by character at a quick,
-- human-looking pace (indentation comes at once, as an editor's would), writes the file, then runs it in a terminal
-- split below. Its progress goes to the status folder, which jarvis/showcase/runner.py polls:
--   progress  0..1 while typing        typed  the line count, once typed      ran   the program has started
--   done      its exit code             error  what went wrong (the runner then goes on without it)
-- The job (livecode.json): {"source": the program, "status": folder, "cps": chars/s, "python": exe, "args": [...]}.
-- Without g:jarvis_livecode it does nothing (a plain Neovim). Check it headless: uv run scripts/livecode_check.py

local uv = vim.uv or vim.loop
local job = {}
local status = nil

local function mark(name, text)
  if not status then
    return
  end
  local f = io.open(status .. '/' .. name, 'w')
  if f then
    f:write(text or '')
    f:close()
  end
end

local function fail(msg)
  mark('error', tostring(msg))
  vim.schedule(function()
    vim.api.nvim_echo({ { 'livecode: ' .. tostring(msg), 'ErrorMsg' } }, true, {})
  end)
end

-- The look: a dark built-in colour scheme, line numbers, nothing that pops up or asks.
local function setup()
  vim.o.termguicolors = true
  if not pcall(vim.cmd.colorscheme, 'catppuccin') then
    pcall(vim.cmd.colorscheme, 'habamax')
  end
  vim.o.number = true
  vim.o.relativenumber = false
  vim.o.cursorline = true
  vim.o.signcolumn = 'no'
  vim.o.showmode = false
  vim.o.ruler = false
  vim.o.laststatus = 0
  vim.o.scrolloff = 6
  vim.o.swapfile = false
  vim.o.shortmess = vim.o.shortmess .. 'IWF'
  vim.o.mouse = ''
  vim.cmd('syntax on')
end

-- Each character's delay (ms): the base pace with a jitter, a breath at the end of a line, a little more around
-- punctuation.
local function delay(ch, prev)
  local base = 1000 / math.max(5, job.cps or 150)
  local d = base * (0.4 + math.random() * 1.2)
  if prev == '\n' then
    d = d + base * (6 + math.random() * 12)
  elseif ch == ' ' and math.random() < 0.12 then
    d = d + base * (6 + math.random() * 13)
  elseif ch:match('[%(%)%[%]{}:,=]') then
    d = d + base * 0.5
  end
  return d
end

local function run_program(file, lines)
  mark('typed', tostring(lines))
  vim.cmd('stopinsert')
  vim.cmd('silent write')
  vim.cmd('botright split')
  vim.cmd('resize ' .. math.max(8, math.floor(vim.o.lines * 0.62)))
  vim.cmd('enew')
  local cmd = { job.python or 'python3', file }
  for _, a in ipairs(job.args or {}) do
    table.insert(cmd, tostring(a))
  end
  local ok, id = pcall(vim.fn.jobstart, cmd, {
    term = true,
    on_exit = function(_, code)
      mark('done', tostring(code))
    end,
  })
  if not ok or id <= 0 then
    fail('could not run the program: ' .. tostring(id))
    return
  end
  mark('ran', tostring(id))
  vim.cmd('normal! G')
end

-- Types `text` into the current buffer. Each character has its own due time on a virtual clock and every tick types
-- whatever is due, so the pace holds whatever the timer's resolution.
local function type_out(text, file)
  local buf = vim.api.nvim_get_current_buf()
  local win = vim.api.nvim_get_current_win()
  local chars = vim.fn.split(text, '\\zs')
  local total = #chars
  local i = 1
  local row, col = 0, 0
  local last_pct = -1
  local function now()
    return uv.hrtime() / 1e6
  end
  local due = now() + delay(chars[1] or '', '')
  local timer = uv.new_timer()

  local function put(ch)
    if ch == '\n' then
      vim.api.nvim_buf_set_text(buf, row, col, row, col, { '', '' })
      row, col = row + 1, 0
      -- the next line's indentation comes at once, as an editor's auto-indent would
      local indent = ''
      while i <= total and chars[i] == ' ' do
        indent = indent .. ' '
        i = i + 1
      end
      if indent ~= '' then
        vim.api.nvim_buf_set_text(buf, row, col, row, col, { indent })
        col = col + #indent
      end
    else
      vim.api.nvim_buf_set_text(buf, row, col, row, col, { ch })
      col = col + #ch
    end
  end

  local function tick()
    local ok, err = pcall(function()
      local t = now()
      local n = 0
      while i <= total and due <= t and n < 16 do
        local ch = chars[i]
        i = i + 1
        put(ch)
        due = due + delay(chars[i] or '', ch)
        n = n + 1
      end
      if n > 0 then
        vim.api.nvim_win_set_cursor(win, { row + 1, col })
        local pct = math.floor((i - 1) * 100 / total)
        if pct >= last_pct + 2 then
          last_pct = pct
          mark('progress', string.format('%.3f', (i - 1) / total))
        end
      end
      if i > total then
        timer:stop()
        timer:close()
        mark('progress', '1')
        run_program(file, vim.api.nvim_buf_line_count(buf))
      end
    end)
    if not ok then
      timer:stop()
      fail(err)
    end
  end

  timer:start(0, 12, vim.schedule_wrap(tick))
end

local function start()
  local path = vim.g.jarvis_livecode
  if type(path) ~= 'string' or path == '' then
    return
  end
  local f = io.open(path, 'r')
  if not f then
    return
  end
  local ok, decoded = pcall(vim.json.decode, f:read('*a'))
  f:close()
  if not ok or type(decoded) ~= 'table' then
    return
  end
  job = decoded
  status = job.status
  math.randomseed(os.time())
  local src = io.open(job.source or '', 'r')
  if not src then
    fail('no program at ' .. tostring(job.source))
    return
  end
  local text = src:read('*a'):gsub('\n+$', '') -- Neovim writes the last line's end itself
  src:close()
  local file = vim.api.nvim_buf_get_name(0)
  local waited = 0
  local poll = uv.new_timer()
  -- type once the window is on screen (the runner's `go`), or after 20 s whatever happens
  poll:start(50, 50, vim.schedule_wrap(function()
    waited = waited + 50
    if uv.fs_stat(status .. '/go') ~= nil or waited >= 20000 then
      poll:stop()
      poll:close()
      vim.cmd('startinsert')
      type_out(text, file)
    end
  end))
end

setup()
vim.api.nvim_create_autocmd('VimEnter', { once = true, callback = start })
