param([switch]$SmokeTest, [string]$SnapshotPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$root = $PSScriptRoot
$python = Join-Path $root 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
  [System.Windows.Forms.MessageBox]::Show('Download the portable Windows ZIP, which includes the application runtime.','Field Manual Viewer') | Out-Null
  exit 1
}
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Field Manual Viewer - Library Setup'
$form.Size = New-Object System.Drawing.Size(860,820)
$form.MinimumSize = New-Object System.Drawing.Size(800,700)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object System.Drawing.Font('Segoe UI',11)
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi
$form.AutoScroll = $true
$form.BackColor = [System.Drawing.ColorTranslator]::FromHtml('#f4f3ed')
$layout = New-Object System.Windows.Forms.TableLayoutPanel
$layout.Dock = 'Top'; $layout.AutoSize = $true
$layout.Padding = New-Object System.Windows.Forms.Padding(24)
$layout.ColumnCount = 1; $layout.RowCount = 10
foreach ($height in @(36,54,30,148,48,48,60,108,60,74)) {
 $layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,$height))) | Out-Null
}
$form.Controls.Add($layout)
function New-Label($text) {
 $label=New-Object System.Windows.Forms.Label
 $label.Text=$text; $label.Dock='Fill'; $label.TextAlign='MiddleLeft'
 return $label
}
$title=New-Label 'FIELD MANUAL VIEWER'
$title.Font=New-Object System.Drawing.Font('Segoe UI',17,[System.Drawing.FontStyle]::Bold)
$title.ForeColor=[System.Drawing.ColorTranslator]::FromHtml('#254c3c')
$layout.Controls.Add($title,0,0)
$headline=New-Label 'Reading your library...'
$headline.Font=New-Object System.Drawing.Font('Segoe UI',15)
$layout.Controls.Add($headline,0,1)
$intro=New-Label ('Library folder: '+$root)
$intro.AutoEllipsis=$true
$folderTip=New-Object System.Windows.Forms.ToolTip
$folderTip.SetToolTip($intro,$root)
$layout.Controls.Add($intro,0,2)
$counts=New-Object System.Windows.Forms.TableLayoutPanel
$counts.Dock='Fill'; $counts.ColumnCount=2; $counts.RowCount=4
foreach($i in 0..1){$counts.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent,50)))|Out-Null}
$countLabels=@()
foreach($i in 0..7){$label=New-Label ''; $countLabels+=,$label; $counts.Controls.Add($label,($i%2),[Math]::Floor($i/2))}
$layout.Controls.Add($counts,0,3)
function New-ProgressRow($text) {
 $panel=New-Object System.Windows.Forms.TableLayoutPanel
 $panel.Dock='Fill'; $panel.ColumnCount=1; $panel.RowCount=2
 $label=New-Label $text
 $bar=New-Object System.Windows.Forms.ProgressBar; $bar.Dock='Fill'; $bar.Maximum=1000
 $panel.Controls.Add($label,0,0); $panel.Controls.Add($bar,0,1)
 return @($panel,$label,$bar)
}
$downloadProgress=New-ProgressRow 'Originals saved'; $layout.Controls.Add($downloadProgress[0],0,4)
$indexProgress=New-ProgressRow 'Search preparation'; $layout.Controls.Add($indexProgress[0],0,5)
$activity=New-Label ''; $activity.AutoEllipsis=$true; $layout.Controls.Add($activity,0,6)
$buttons=New-Object System.Windows.Forms.FlowLayoutPanel
$buttons.Dock='Fill'; $buttons.WrapContents=$true
function New-Button($text) {
 $button=New-Object System.Windows.Forms.Button
 $button.Text=$text; $button.AutoSize=$true
 $button.MinimumSize=New-Object System.Drawing.Size(145,46)
 $button.Margin=New-Object System.Windows.Forms.Padding(0,4,10,4)
 return $button
}
$primary=New-Button 'Download All'
$primary.BackColor=[System.Drawing.ColorTranslator]::FromHtml('#254c3c'); $primary.ForeColor=[System.Drawing.Color]::White
$test=New-Button 'Download Test Batch (20)'
$pause=New-Button 'Pause / Stop'; $open=New-Button 'Open Field Manual Viewer'; $advanced=New-Button 'Advanced'
foreach($button in @($primary,$test,$pause,$open,$advanced)){$buttons.Controls.Add($button)}
$layout.Controls.Add($buttons,0,7)
$warning=New-Object System.Windows.Forms.TextBox
$warning.ReadOnly=$true; $warning.Multiline=$true; $warning.ScrollBars='Vertical'
$warning.Dock='Fill'; $warning.BackColor=$form.BackColor; $warning.BorderStyle='None'
$layout.Controls.Add($warning,0,8)
$footer=New-Label 'Closing setup pauses work safely. Reopen it to resume.'
$footer.Font=New-Object System.Drawing.Font('Segoe UI',9)
$footerPanel=New-Object System.Windows.Forms.TableLayoutPanel
$footerPanel.Dock='Fill'; $footerPanel.ColumnCount=1; $footerPanel.RowCount=3
$sourceLink=New-Object System.Windows.Forms.LinkLabel
$sourceLink.Text='Thanks to T.REX ARMS - visit their live Field Manual Vault (internet required)'
$sourceLink.Font=$footer.Font; $sourceLink.Dock='Fill'; $sourceLink.TextAlign='MiddleLeft'
$sourceLink.Add_LinkClicked({Start-Process 'https://www.trex-arms.com/field-manual-vault' | Out-Null})
$independence=New-Label 'Independent project. Not affiliated with or endorsed by T.REX ARMS.'
$independence.Font=$footer.Font
$footerPanel.Controls.Add($sourceLink,0,0); $footerPanel.Controls.Add($independence,0,1); $footerPanel.Controls.Add($footer,0,2)
$layout.Controls.Add($footerPanel,0,9)
$script:statusProcess=$null; $script:commandProcess=$null; $script:snapshot=$null
$script:closing=$false; $script:allowClose=$false; $script:closeProcess=$null; $script:closeReady=$false
$script:lastPoll=[DateTime]::MinValue
$statusFile="reports/setup-status-$PID.json"; $responseFile="reports/setup-response-$PID.json"; $closeFile="reports/setup-close-$PID.json"
function New-PythonProcess($arguments) {
 $info=New-Object System.Diagnostics.ProcessStartInfo
 $info.FileName=$python; $info.Arguments=$arguments; $info.WorkingDirectory=$root
 $info.UseShellExecute=$false; $info.CreateNoWindow=$true
 return [System.Diagnostics.Process]::Start($info)
}
function Request-Operation($action,$confirmText) {
 if($script:commandProcess -or ($script:snapshot -and $script:snapshot.active)){return}
 if($confirmText){
  $answer=[System.Windows.Forms.MessageBox]::Show($form,$confirmText,'Prepare your offline library',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Question)
  if($answer -ne 'Yes'){return}
 }
 $script:commandProcess=New-PythonProcess "-m field_manual_viewer.setup_control start $action --authorize --output $responseFile"
 $primary.Enabled=$false; $test.Enabled=$false; $activity.Text='Starting safely...'
}
$primary.Add_Click({
 if($script:snapshot -and $script:snapshot.state -eq 'repair_pending'){Request-Operation 'repair' 'Retrieve missing or damaged originals from the publisher? Verified originals will be retained. This operation requires internet access.'; return}
 if($script:snapshot -and $script:snapshot.state -eq 'verify_pending'){Request-Operation 'verify' ''; return}
 if($script:snapshot -and $script:snapshot.state -eq 'index_pending'){Request-Operation 'index' ''; return}
 $free=if($script:snapshot){[Math]::Round($script:snapshot.free_bytes/1GB)}else{0}
 if($free -lt 21){[System.Windows.Forms.MessageBox]::Show($form,'Not enough free space to start safely. The reference library is about 70 GB; plan for about 100 GB free, including working room and the reserve.','Storage check')|Out-Null; return}
 $message="Download and prepare the public collection in this folder?`r`n$root`r`n`r`nThis can take several hours. The reference library is about 70 GB. Plan for about 100 GB free, including working room and the reserve. You currently have $free GB free. Setup pauses before using its 20 GB reserve.`r`n`r`nVerified files are retained. A new library tests 20 publications before continuing."
 $recommended=if($script:snapshot){[Math]::Ceiling($script:snapshot.recommended_free_bytes/1GB)}else{94}
 if($script:snapshot -and -not $script:snapshot.storage_sufficient){
  $choice=[System.Windows.Forms.MessageBox]::Show($form,"This drive has $free GB free; the current estimate recommends at least $recommended GB free to finish. Setup will probably pause before completion. Free space or choose another drive if possible.`r`n`r`nContinue anyway, keeping already downloaded files?",'Insufficient space for estimated library',[System.Windows.Forms.MessageBoxButtons]::YesNo,[System.Windows.Forms.MessageBoxIcon]::Warning,[System.Windows.Forms.MessageBoxDefaultButton]::Button2)
  if($choice -ne 'Yes'){return}
 }
 Request-Operation 'download' $message
})
$test.Add_Click({Request-Operation 'test' 'Download only a test batch of 20 publications, each as PDF and EPUB, then stop? The first 20 publications start downloading as soon as their catalog entries are found. Remaining catalog metadata is read alongside the test downloads. You can choose Download All later; it will retain these files and their search indexes without downloading them again.'})
$pause.Add_Click({
 if(-not $script:commandProcess){
  $script:commandProcess=New-PythonProcess "-m field_manual_viewer.setup_control pause --output $responseFile"
  $activity.Text='Pause requested. Saved files and partial transfers will be retained.'
 }
})
$open.Add_Click({Start-Process -FilePath (Join-Path $root 'runtime\python\pythonw.exe') -ArgumentList '-m field_manual_viewer.launcher' -WorkingDirectory $root -WindowStyle Hidden | Out-Null})
$menu=New-Object System.Windows.Forms.ContextMenuStrip
function Add-MenuItem($text,$handler){$item=$menu.Items.Add($text); $item.Add_Click($handler)}
Add-MenuItem 'Resume search preparation (offline)' {Request-Operation 'index' ''}
Add-MenuItem 'Check saved file integrity (offline)' {Request-Operation 'verify' ''}
Add-MenuItem 'Repair missing or failed originals...' {Request-Operation 'repair' 'Attempt to retrieve missing, damaged or failed originals from the publisher? Existing verified files are retained. Source exceptions may remain unavailable.'}
Add-MenuItem 'Refresh source catalog (exceptional recovery)...' {Request-Operation 'refresh' 'Contact the publisher to refresh the catalog and acquire missing documents? This optional recovery is not needed by a completed offline library.'}
Add-MenuItem 'Review errors' {
 $text=if($script:snapshot -and $script:snapshot.issues.Count){($script:snapshot.issues|ForEach-Object{"$($_.id) [$($_.format)]: $($_.error)"}) -join "`r`n"}else{'No file exceptions are currently recorded.'}
 [System.Windows.Forms.MessageBox]::Show($form,$text,'Library exceptions')|Out-Null
}
Add-MenuItem 'Open technical logs' {
 $path=Join-Path $root 'reports'
 if(-not(Test-Path -LiteralPath $path)){New-Item -ItemType Directory -Path $path|Out-Null}
 Start-Process explorer.exe -ArgumentList ('"'+$path+'"')|Out-Null
}
Add-MenuItem 'Stop local viewer server before copying' {
 Start-Process -FilePath (Join-Path $root 'runtime\python\pythonw.exe') -ArgumentList '-m field_manual_viewer.launcher --stop' -WorkingDirectory $root -WindowStyle Hidden | Out-Null
}
$advanced.Add_Click({$menu.Show($advanced,0,$advanced.Height)})
function Apply-Status($s) {
 $script:snapshot=$s; $headline.Text=$s.message
 $countLabels[0].Text="Publications found: $($s.discovered) / $($s.catalog_expected)"; $countLabels[1].Text="Publications offline: $($s.available)"
 $countLabels[2].Text="PDFs verified: $($s.pdf_verified)"; $countLabels[3].Text="EPUBs verified: $($s.epub_verified)"
 $countLabels[4].Text="PDFs searchable: $($s.pdf_indexed)"; $countLabels[5].Text="EPUBs searchable: $($s.epub_indexed)"
 $countLabels[6].Text="Publications needing files: $($s.outstanding_publications)"; $countLabels[7].Text="Free space: $([Math]::Round($s.free_bytes/1GB)) GB"
 $saved=$s.pdf_verified+$s.epub_verified; $indexed=$s.pdf_indexed+$s.epub_indexed; $expected=$s.discovered*2
 if($s.test_mode){$saved=$s.test_files_verified; $indexed=$s.test_files_indexed; $expected=40}
 $downloadProgress[1].Text=if($s.test_mode){"Test PDF + EPUB files: $saved / 40 (20 publications)"}else{"PDF + EPUB files saved: $saved / $expected"}
 $downloadProgress[2].Value=[Math]::Min(1000,[int](1000*$saved/[Math]::Max(1,$expected)))
 $indexProgress[1].Text="Search prepared: $indexed / $saved saved files"
 $indexProgress[2].Value=[Math]::Min(1000,[int](1000*$indexed/[Math]::Max(1,$saved)))
 $primary.Visible=$s.state -notin @('complete','exceptions')
 $primary.Enabled=-not $s.active -and -not $script:commandProcess
 $primary.Text=if($s.state -eq 'repair_pending'){'Repair missing files'}elseif($s.state -eq 'verify_pending'){'Check saved files'}elseif($s.state -eq 'index_pending'){'Resume indexing'}elseif($s.test_ready){'Download All'}elseif($s.available -gt 0){'Resume Download'}else{'Download All'}
 $test.Visible=$s.state -notin @('complete','exceptions')
 $test.Enabled=-not $s.active -and -not $script:commandProcess -and -not $s.test_ready
 $test.Text=if($s.test_ready){'Test Batch Ready (20)'}elseif($s.test_started){'Resume Test Batch (20)'}else{'Download Test Batch (20)'}
 $pause.Visible=[bool]$s.active; $pause.Enabled=[bool]$s.active; $advanced.Enabled=-not $s.active; $activity.Text=$s.message
 if($s.active -and -not $s.catalog_complete -and $s.phase -ne 'discovering'){$activity.Text=$s.message+' | '+$s.catalog_message}
 $notes=@(); if($s.last_error){$notes+=,$s.last_error}
 if($s.issues.Count){$notes+=,"$($s.issues.Count) recorded exceptions. Use Advanced > Review errors for details."}
 $warning.Text=$notes -join "`r`n"
 if($script:closing){Show-ClosingStatus}
}
function Show-ClosingStatus {
 $headline.Text='Pausing safely before closing...'
 $activity.Text='Finishing the current request or document. Your saved progress will be retained.'
 foreach($button in @($primary,$test,$pause,$open,$advanced)){$button.Enabled=$false}
}
$form.Add_FormClosing({param($sender,$eventArgs)
 if($script:allowClose){return}
 $eventArgs.Cancel=$true
 if($script:closing){return}
 $script:closing=$true; $script:closeReady=$false
 Show-ClosingStatus
 try {$script:closeProcess=New-PythonProcess "-m field_manual_viewer.setup_control close --output $closeFile"}
 catch {$script:closing=$false; $warning.Text='Could not request a safe pause. '+$_.Exception.Message; $script:lastPoll=[DateTime]::MinValue}
})
$timer=New-Object System.Windows.Forms.Timer; $timer.Interval=600
$timer.Add_Tick({
 try {
  if($script:commandProcess -and $script:commandProcess.HasExited){
   $script:commandProcess.Dispose(); $script:commandProcess=$null
   $path=Join-Path $root $responseFile
   if(Test-Path -LiteralPath $path){$response=Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json; if($response.error){$warning.Text=$response.error}else{$activity.Text=$response.message}}
   $script:lastPoll=[DateTime]::MinValue
  }
  if($script:statusProcess -and $script:statusProcess.HasExited){
   $script:statusProcess.Dispose(); $script:statusProcess=$null
   $path=Join-Path $root $statusFile
   if(Test-Path -LiteralPath $path){$s=Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json; if($s.error){$warning.Text=$s.error}else{Apply-Status $s}}
  }
  if($script:closing){
   if($script:closeProcess -and $script:closeProcess.HasExited){
    $code=$script:closeProcess.ExitCode; $script:closeProcess.Dispose(); $script:closeProcess=$null
    $path=Join-Path $root $closeFile
    $response=if(Test-Path -LiteralPath $path){Get-Content -LiteralPath $path -Raw -Encoding UTF8|ConvertFrom-Json}else{$null}
    if($code -ne 0 -or -not $response.closed_safely){
     $script:closing=$false; $script:lastPoll=[DateTime]::MinValue
     $warning.Text='Could not confirm a safe pause. '+$response.error
     [System.Windows.Forms.MessageBox]::Show($form,$warning.Text,'Library Setup remains open')|Out-Null
    }else{$script:closeReady=$true}
   }
   if($script:closing){
    Show-ClosingStatus
    if($script:closeReady -and -not $script:commandProcess -and -not $script:statusProcess){$script:allowClose=$true; $form.Close()}
    return
   }
  }
  if(-not $script:statusProcess -and ([DateTime]::Now-$script:lastPoll).TotalSeconds -ge 3){
   $script:lastPoll=[DateTime]::Now; $script:statusProcess=New-PythonProcess "-m field_manual_viewer.setup_control status --output $statusFile"
  }
 } catch {
  $warning.Text='Could not refresh status. '+$_.Exception.Message
  if($script:closing -and -not $script:closeProcess -and -not $script:closeReady){$script:closing=$false; $script:lastPoll=[DateTime]::MinValue}
 }
})
$form.Add_Shown({$timer.Start()})
$form.Add_FormClosed({$timer.Stop(); $timer.Dispose()})
if($SmokeTest){
 $testStatus=& $python -m field_manual_viewer.setup_control status | ConvertFrom-Json
 Apply-Status $testStatus
 $form.Show(); [System.Windows.Forms.Application]::DoEvents()
 $until=[DateTime]::Now.AddSeconds(4)
 while([DateTime]::Now -lt $until){[System.Windows.Forms.Application]::DoEvents();Start-Sleep -Milliseconds 40}
 if($warning.Text -like 'Could not refresh status*'){throw $warning.Text}
 if($SnapshotPath){
  $bitmap=New-Object System.Drawing.Bitmap($form.Width,$form.Height)
  $form.DrawToBitmap($bitmap,(New-Object System.Drawing.Rectangle(0,0,$form.Width,$form.Height)))
  $bitmap.Save($SnapshotPath,[System.Drawing.Imaging.ImageFormat]::Png); $bitmap.Dispose()
 }
 Write-Output ("GUI constructed: " + $testStatus.state + "; primary action visible=" + $primary.Visible)
 $form.Close()
 $closeDeadline=[DateTime]::Now.AddSeconds(60)
 while(-not $form.IsDisposed -and [DateTime]::Now -lt $closeDeadline){[System.Windows.Forms.Application]::DoEvents();Start-Sleep -Milliseconds 40}
 if(-not $form.IsDisposed){throw 'Setup did not complete its safe-close handshake.'}
 exit 0
}
[System.Windows.Forms.Application]::Run($form)
