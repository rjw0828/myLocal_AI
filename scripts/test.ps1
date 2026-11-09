[CmdletBinding()]
param(
    [ValidateSet("quick", "integration", "full")]
    [string]$Mode = "quick",

    [ValidateSet("app", "sessions", "storage", "voice", "ui", "ollama", "mcp")]
    [string[]]$Module,

    # Runner diagnostics only. These do not execute project tests.
    [ValidateSet("none", "failure", "zero")]
    [string]$RunnerProbe = "none"
)

$ErrorActionPreference = "Stop"
$script:startedAt = Get-Date
$script:totalTests = 0
$script:failed = $false
$script:failureDetails = New-Object System.Collections.Generic.List[string]

function Get-RepositoryRoot {
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}

function Get-ChangedPaths {
    param([string]$RepositoryRoot)

    $paths = New-Object System.Collections.Generic.List[string]
    $tracked = & git -C $RepositoryRoot diff --name-only HEAD 2>$null
    if ($LASTEXITCODE -eq 0) {
        foreach ($path in $tracked) {
            if ($path) { $paths.Add(($path -replace "\\", "/")) }
        }
    }
    $untracked = & git -C $RepositoryRoot ls-files --others --exclude-standard 2>$null
    if ($LASTEXITCODE -eq 0) {
        foreach ($path in $untracked) {
            if ($path) { $paths.Add(($path -replace "\\", "/")) }
        }
    }
    return @($paths | Select-Object -Unique)
}

function Get-InferredModules {
    param([string[]]$Paths)

    $selected = New-Object System.Collections.Generic.List[string]
    foreach ($path in $Paths) {
        switch -Regex ($path) {
            '^(mcp_manager/|tests/test_mcp_)' { $selected.Add("mcp"); continue }
            '^(storage/|tests/test_sqlite_store\.py)' { $selected.Add("storage"); continue }
            '^tests/(test_sessions|test_delete_all)\.py' { $selected.Add("sessions"); continue }
            '^(voice/|tests/test_(stt|tts)_api\.py)' { $selected.Add("voice"); continue }
            '^(static/|templates/|tests/test_voice_(turn|ui)\.js)' { $selected.Add("ui"); continue }
            '^(ollama_runtime\.py|tests/test_ollama_runtime\.py)' { $selected.Add("ollama"); continue }
            '^(app\.py|ai_controller\.py|tests/test_app\.py)' { $selected.Add("app"); continue }
        }
    }
    return @($selected | Select-Object -Unique)
}

function Add-UniqueItem {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Value
    )
    if (-not $List.Contains($Value)) { $List.Add($Value) }
}

function Get-TestPlan {
    param(
        [string]$SelectedMode,
        [string[]]$SelectedModules
    )

    $python = New-Object System.Collections.Generic.List[string]
    $javascript = New-Object System.Collections.Generic.List[string]

    if ($SelectedMode -eq "full") {
        if ($SelectedModules -and $SelectedModules.Count -gt 0) {
            throw "-Module cannot be combined with -Mode full because full must not omit tests."
        }
        return [pscustomobject]@{
            DiscoverPython = $true
            Python = @()
            JavaScript = @("tests/test_voice_turn.js", "tests/test_voice_ui.js")
        }
    }

    foreach ($name in $SelectedModules) {
        switch ($name) {
            "app" {
                Add-UniqueItem $python "tests/test_app.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_sessions.py"
                    Add-UniqueItem $python "tests/test_sqlite_store.py"
                    Add-UniqueItem $python "tests/test_ollama_runtime.py"
                }
            }
            "sessions" {
                Add-UniqueItem $python "tests/test_sessions.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_delete_all.py"
                    Add-UniqueItem $python "tests/test_sqlite_store.py"
                }
            }
            "storage" {
                Add-UniqueItem $python "tests/test_sqlite_store.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_sessions.py"
                    Add-UniqueItem $python "tests/test_delete_all.py"
                }
            }
            "voice" {
                Add-UniqueItem $python "tests/test_stt_api.py"
                Add-UniqueItem $python "tests/test_tts_api.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $javascript "tests/test_voice_turn.js"
                    Add-UniqueItem $javascript "tests/test_voice_ui.js"
                }
            }
            "ui" {
                Add-UniqueItem $javascript "tests/test_voice_turn.js"
                Add-UniqueItem $javascript "tests/test_voice_ui.js"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_app.py"
                }
            }
            "ollama" {
                Add-UniqueItem $python "tests/test_ollama_runtime.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_app.py"
                }
            }
            "mcp" {
                Add-UniqueItem $python "tests/test_mcp_runtime.py"
                Add-UniqueItem $python "tests/test_mcp_registry.py"
                Add-UniqueItem $python "tests/test_mcp_api.py"
                if ($SelectedMode -eq "integration") {
                    Add-UniqueItem $python "tests/test_mcp_stdio.py"
                    Add-UniqueItem $python "tests/test_mcp_api_stdio.py"
                }
            }
        }
    }

    return [pscustomobject]@{
        DiscoverPython = $false
        Python = @($python)
        JavaScript = @($javascript)
    }
}

function Get-CoreFailureLines {
    param([string]$Text)

    $lines = @($Text -split "`r?`n")
    $important = @($lines | Where-Object {
        $_ -match '^(FAIL|ERROR|FAILED|Traceback|AssertionError|.*(?:Error|Exception):)' -or
        $_ -match '^test_.*\.\.\. (FAIL|ERROR)$'
    } | Select-Object -First 20)
    if ($important.Count -eq 0) {
        $important = @($lines | Where-Object { $_.Trim() } | Select-Object -Last 20)
    }
    return $important
}

function Invoke-TestCommand {
    param(
        [string]$Name,
        [string]$Executable,
        [string[]]$Arguments,
        [ValidateSet("python", "javascript")]
        [string]$Kind,
        [string]$LogPath
    )

    $commandStarted = Get-Date
    try {
        $previousErrorAction = $ErrorActionPreference
        $previousPythonPath = $env:PYTHONPATH
        if ($Kind -eq "python") {
            $testPath = Join-Path $script:repositoryRoot "tests"
            if ($previousPythonPath) { $env:PYTHONPATH = "$testPath$([System.IO.Path]::PathSeparator)$previousPythonPath" }
            else { $env:PYTHONPATH = $testPath }
        }
        $ErrorActionPreference = "Continue"
        try {
            & $Executable @Arguments *> $LogPath
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorAction
            if ($Kind -eq "python") { $env:PYTHONPATH = $previousPythonPath }
        }
    }
    catch {
        $exitCode = 1
        $_ | Out-String | Set-Content -LiteralPath $LogPath -Encoding UTF8
    }
    $elapsed = ((Get-Date) - $commandStarted).TotalSeconds
    $text = ""
    if (Test-Path -LiteralPath $LogPath) {
        $text = Get-Content -LiteralPath $LogPath -Raw -ErrorAction SilentlyContinue
    }

    $count = 0
    if ($Kind -eq "python") {
        $matches = [regex]::Matches($text, 'Ran\s+(\d+)\s+tests?\s+in')
        if ($matches.Count -gt 0) {
            $count = [int]$matches[$matches.Count - 1].Groups[1].Value
        }
    }
    else {
        $matches = [regex]::Matches($text, '(\d+)\s*/\s*(\d+).*tests passed')
        if ($matches.Count -gt 0) {
            $count = [int]$matches[$matches.Count - 1].Groups[2].Value
        }
    }

    $valid = ($exitCode -eq 0 -and $count -gt 0)
    if (-not $valid) {
        $script:failed = $true
        $reason = "exit=$exitCode tests=$count"
        if ($exitCode -eq 0 -and $count -eq 0) { $reason = "zero tests detected" }
        $script:failureDetails.Add("[$Name] $reason")
        foreach ($line in (Get-CoreFailureLines $text)) {
            $script:failureDetails.Add("  $line")
        }
    }
    $script:totalTests += $count
    return [pscustomobject]@{ Name = $Name; Count = $count; Seconds = $elapsed; ExitCode = $exitCode }
}

try {
    $repositoryRoot = Get-RepositoryRoot
    $script:repositoryRoot = $repositoryRoot
    Set-Location -LiteralPath $repositoryRoot

    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($repositoryRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Repository must not contain the external test log directory."
    }
    $runName = "{0}-{1}-{2}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID, $Mode
    $logDirectory = Join-Path $tempRoot (Join-Path "LocalAI-TestLogs" $runName)
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

    $pythonExecutable = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
        throw "Project Python was not found: $pythonExecutable"
    }

    if ($RunnerProbe -ne "none") {
        if ($RunnerProbe -eq "failure") {
            $code = "import sys; print('test_runner_probe ... FAIL'); print('AssertionError: intentional runner probe'); print('Ran 1 test in 0.001s'); print('FAILED (failures=1)'); sys.exit(1)"
        }
        else {
            $code = "print('Ran 0 tests in 0.000s'); print('OK')"
        }
        $null = Invoke-TestCommand -Name "runner-$RunnerProbe" -Executable $pythonExecutable `
            -Arguments @("-c", $code) -Kind python -LogPath (Join-Path $logDirectory "runner-probe.log")
    }
    else {
        $selectedModules = @($Module | Where-Object { $_ })
        if ($Mode -ne "full" -and $selectedModules.Count -eq 0) {
            $selectedModules = @(Get-InferredModules (Get-ChangedPaths $repositoryRoot))
            if ($selectedModules.Count -eq 0) {
                if ($Mode -eq "quick") { $selectedModules = @("app") }
                else { $selectedModules = @("app", "sessions", "storage") }
            }
        }

        $plan = Get-TestPlan -SelectedMode $Mode -SelectedModules $selectedModules
        if (-not $plan.DiscoverPython -and $plan.Python.Count -eq 0 -and $plan.JavaScript.Count -eq 0) {
            throw "The selected test plan contains no test commands."
        }

        if ($plan.DiscoverPython) {
            $null = Invoke-TestCommand -Name "python-full" -Executable $pythonExecutable `
                -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v") -Kind python `
                -LogPath (Join-Path $logDirectory "python-full.log")
        }
        elseif ($plan.Python.Count -gt 0) {
            $pythonArguments = @("-m", "unittest", "-v") + @($plan.Python)
            $null = Invoke-TestCommand -Name "python-$Mode" -Executable $pythonExecutable `
                -Arguments $pythonArguments -Kind python -LogPath (Join-Path $logDirectory "python-$Mode.log")
        }

        if ($plan.JavaScript.Count -gt 0) {
            $node = Get-Command node -ErrorAction SilentlyContinue
            if (-not $node) { throw "Node.js was not found but JavaScript tests were selected." }
            $index = 0
            foreach ($javascriptTest in $plan.JavaScript) {
                $index += 1
                $null = Invoke-TestCommand -Name ([System.IO.Path]::GetFileName($javascriptTest)) `
                    -Executable $node.Source -Arguments @($javascriptTest) -Kind javascript `
                    -LogPath (Join-Path $logDirectory ("javascript-{0}.log" -f $index))
            }
        }
    }
}
catch {
    $script:failed = $true
    $script:failureDetails.Add($_.Exception.Message)
}

$duration = ((Get-Date) - $script:startedAt).TotalSeconds
if ($script:failed -or $script:totalTests -eq 0) {
    if (-not $script:failed) { $script:failureDetails.Add("No tests were executed.") }
    Write-Output ("FAIL mode={0} tests={1} duration={2:N2}s" -f $Mode, $script:totalTests, $duration)
    foreach ($detail in $script:failureDetails) { Write-Output $detail }
    if ($logDirectory) { Write-Output "Log: $logDirectory" }
    exit 1
}

Write-Output ("PASS mode={0} tests={1} duration={2:N2}s" -f $Mode, $script:totalTests, $duration)
exit 0
