[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$TargetDir = (Join-Path ([System.IO.Path]::GetTempPath()) "craftsman-demo")
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    [Console]::Error.WriteLine("Error: Target directory path must not be empty.")
    exit 1
}

function Resolve-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        $Path = Join-Path $PWD.Path $Path
    }
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($root -and $full.Length -gt $root.Length) {
        $full = $full.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    }

    $cursor = $full
    $tail = @()
    while ($cursor -and -not (Test-Path -LiteralPath $cursor)) {
        $parent = Split-Path -Path $cursor -Parent
        if (-not $parent -or $parent -eq $cursor) {
            break
        }
        $leaf = Split-Path -Path $cursor -Leaf
        $tail = @($leaf) + $tail
        $cursor = $parent
    }
    if ($cursor -and (Test-Path -LiteralPath $cursor)) {
        $resolved = (Resolve-Path -LiteralPath $cursor).ProviderPath
        if ($root -and $resolved.Length -gt $root.Length) {
            $resolved = $resolved.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
        }
        foreach ($part in $tail) {
            $resolved = Join-Path $resolved $part
        }
        $full = $resolved
    }
    return $full
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..\..")).ProviderPath
$RealTarget = Resolve-CanonicalPath -Path $TargetDir
$RealRepo = Resolve-CanonicalPath -Path $RepoRoot

Write-Host "Setting up Craftsman demo repository in: $RealTarget"

# Ensure target directory is outside the Craftsman worktree to avoid nested repos.
$IsWindowsPlatform = ($env:OS -eq "Windows_NT") -or ($PSVersionTable.Platform -eq "Win32NT")
$Comparison = if ($IsWindowsPlatform) { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
$RepoPrefix = if ($RealRepo.EndsWith([string][System.IO.Path]::DirectorySeparatorChar)) {
    $RealRepo
} else {
    $RealRepo + [System.IO.Path]::DirectorySeparatorChar
}

if ($RealTarget.Equals($RealRepo, $Comparison) -or $RealTarget.StartsWith($RepoPrefix, $Comparison)) {
    [Console]::Error.WriteLine("Error: Target directory must be outside the craftsman repository worktree to prevent nested git repos.")
    exit 1
}

# Only a directory this script created earlier (it holds the marker) is ever deleted.
$Marker = ".craftsman-demo"
$MarkerPath = Join-Path $RealTarget $Marker
if (Test-Path -LiteralPath $RealTarget) {
    if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {
        Remove-Item -LiteralPath $RealTarget -Recurse -Force
    } elseif ((Test-Path -LiteralPath $RealTarget -PathType Container) -and (@(Get-ChildItem -LiteralPath $RealTarget -Force).Count -eq 0)) {
        Remove-Item -LiteralPath $RealTarget -Force
    } else {
        [Console]::Error.WriteLine("Error: $RealTarget already exists and was not created by this script; pick a new path.")
        exit 1
    }
}

New-Item -ItemType Directory -Path $RealTarget -Force | Out-Null
New-Item -ItemType File -Path (Join-Path $RealTarget $Marker) -Force | Out-Null

# Copy demo project files.
Copy-Item -LiteralPath (Join-Path $ScriptDir "shop") -Destination $RealTarget -Recurse
Copy-Item -LiteralPath (Join-Path $ScriptDir "tests") -Destination $RealTarget -Recurse
Copy-Item -LiteralPath (Join-Path $ScriptDir "README.md") -Destination $RealTarget
Copy-Item -LiteralPath (Join-Path $ScriptDir ".gitignore") -Destination $RealTarget

Push-Location -LiteralPath $RealTarget
try {
    Invoke-Checked -FilePath "git" -Arguments @("init", "-b", "main")
    $GitName = if ($env:GIT_AUTHOR_NAME) { $env:GIT_AUTHOR_NAME } else { "Demo User" }
    $GitEmail = if ($env:GIT_AUTHOR_EMAIL) { $env:GIT_AUTHOR_EMAIL } else { "demo@example.com" }
    Invoke-Checked -FilePath "git" -Arguments @("config", "user.name", $GitName)
    Invoke-Checked -FilePath "git" -Arguments @("config", "user.email", $GitEmail)

    Invoke-Checked -FilePath "git" -Arguments @("add", ".")
    Invoke-Checked -FilePath "git" -Arguments @("commit", "-m", "feat(catalog): initial product catalog implementation")

    if (Get-Command craftsman -ErrorAction SilentlyContinue) {
        Invoke-Checked -FilePath "craftsman" -Arguments @("install", "--agents-workspace", ".")
        Invoke-Checked -FilePath "git" -Arguments @("add", ".")
        Invoke-Checked -FilePath "git" -Arguments @("commit", "-m", "chore: configure craftsman workspace")
        Write-Host "Craftsman workspace configuration installed."
    } else {
        Write-Host "Note: 'craftsman' CLI not found on PATH. Run 'pip install craftsman-gate' or 'pip install -e .' first."
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "=== Demo Repository Ready ==="
Write-Host "Location: $RealTarget"
Write-Host ""
Write-Host "Try running:"
Write-Host "  cd `"$RealTarget`""
Write-Host "  python -m unittest discover -s tests"
Write-Host ""
Write-Host "Then prompt your AI agent:"
Write-Host "  /craftsman Add 1-based pagination to catalog search results (page number and optional page size)"
Write-Host ""
