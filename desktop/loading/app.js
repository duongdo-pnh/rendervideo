
  const detail = document.getElementById('detail');
  const title = document.getElementById('title');
  const installButton = document.getElementById('install');
  const retryButton = document.getElementById('retry');
  const spinner = document.querySelector('.spinner');
  const progressPanel = document.getElementById('progress-panel');
  const progressBar = document.getElementById('install-progress');
  const progressCount = document.getElementById('progress-count');
  const stage = document.getElementById('stage');
  const elapsed = document.getElementById('elapsed');
  const logView = document.getElementById('install-log');
  let generation = 0;
  let running = false;
  function showSnapshot(snapshot) {
    const completed = Math.max(0, Math.min(8, Number(snapshot.completed) || 0));
    progressBar.value = completed;
    progressCount.textContent = `${completed}/8 bước hoàn tất`;
    stage.textContent = snapshot.label;
    const text = snapshot.log.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\r/g, '\n').split('\n').slice(-300).join('\n');
    const follow = logView.scrollHeight - logView.scrollTop - logView.clientHeight < 40;
    if (text && logView.textContent !== text) {
      logView.textContent = text;
      if (follow) logView.scrollTop = logView.scrollHeight;
    }
  }
  async function start(install = false) {
    if (running) return;
    running = true;
    const current = ++generation;
    const started = Date.now();
    let polling = false;
    let timer;
    progressPanel.hidden = !install;
    if (install) {
      progressBar.value = 0;
      progressCount.textContent = '0/8 bước hoàn tất';
      stage.textContent = 'Đang chuẩn bị bộ cài…';
      logView.textContent = 'Đang chờ nhật ký…';
      elapsed.textContent = 'Đã chạy 00:00';
    }
    async function refresh() {
      try {
        const snapshot = await window.__TAURI__.core.invoke('get_install_progress');
        if (current === generation) showSnapshot(snapshot);
      } catch (_) {
        // A status read must never interrupt the installer; retry next second.
      }
    }
    installButton.hidden = retryButton.hidden = true;
    spinner.hidden = false;
    title.textContent = install ? 'Đang cài môi trường…' : 'Đang khởi động Byscom Render…';
    detail.textContent = install
      ? 'Đang cài môi trường LatentSync và MuseTalk: Python, thư viện GPU và model cho cả hai. Quá trình có thể mất nhiều thời gian.\nVui lòng giữ ứng dụng mở và xác nhận quyền quản trị khi hệ thống yêu cầu.\nNhật ký: ~/.local/share/byscom-render/logs/setup.log'
      : 'Đang kiểm tra môi trường và chờ backend sẵn sàng';
    try {
      const operation = window.__TAURI__.core.invoke('start_environment', { install });
      if (install) timer = setInterval(() => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        elapsed.textContent = `Đã chạy ${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
        if (!polling) {
          polling = true;
          refresh().finally(() => { polling = false; });
        }
      }, 1000);
      const status = await operation;
      if (install) await refresh();
      if (status === 'reboot') {
        stage.textContent = 'Tạm dừng — cần khởi động lại máy để tiếp tục';
        title.textContent = 'Cần khởi động lại máy';
        detail.textContent = 'Đã chạy bước cài driver NVIDIA. Hãy khởi động lại máy, hoàn tất xác nhận Secure Boot/MOK nếu được yêu cầu, rồi mở ứng dụng và bấm Cài môi trường để tiếp tục.';
      } else if (status === 'missing') {
        title.textContent = 'Cần cài môi trường lần đầu';
        detail.textContent = 'Môi trường chưa có hoặc cài chưa hoàn tất. Bấm Cài môi trường để cài cả LatentSync và MuseTalk, gồm Python, thư viện GPU và model cho từng engine. Cần kết nối Internet và ít nhất 60 GiB trống tại thư mục cài. Nếu thiếu driver NVIDIA, bộ cài sẽ cài driver và yêu cầu khởi động lại máy. Cài xong ứng dụng sẽ tự mở.';
        installButton.textContent = 'Cài môi trường';
        installButton.hidden = false;
      }
    } catch (error) {
      if (install) await refresh();
      stage.textContent = 'Cài đặt chưa hoàn tất — xem nhật ký bên dưới';
      title.textContent = install ? 'Cài môi trường chưa hoàn tất' : 'Không thể khởi động';
      detail.textContent = String(error);
      installButton.textContent = 'Cài lại môi trường';
      installButton.hidden = retryButton.hidden = false;
    } finally {
      clearInterval(timer);
      generation++;
      running = false;
      spinner.hidden = true;
    }
  }
  installButton.addEventListener('click', () => start(true));
  retryButton.addEventListener('click', () => start(false));
  start();
