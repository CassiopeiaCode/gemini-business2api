"""
全局浏览器启动失败计数器
用于追踪所有自动化模块的浏览器启动失败次数
"""
import threading


class BrowserFailureTracker:
    """浏览器启动失败追踪器（单例）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._failure_count = 0
                    cls._instance._max_failures = 5
                    cls._instance._counter_lock = threading.Lock()
        return cls._instance
    
    def record_failure(self) -> int:
        """
        记录一次浏览器启动失败
        
        Returns:
            当前失败总次数
            
        Raises:
            SystemExit: 如果失败次数超过阈值
        """
        with self._counter_lock:
            self._failure_count += 1
            current_count = self._failure_count
            
            if current_count > self._max_failures:
                raise SystemExit(
                    f"浏览器启动失败次数已达到全局上限 ({current_count}/{self._max_failures})，系统退出"
                )
            
            return current_count
    
    def reset(self) -> None:
        """重置失败计数器"""
        with self._counter_lock:
            self._failure_count = 0
    
    def get_count(self) -> int:
        """获取当前失败次数"""
        with self._counter_lock:
            return self._failure_count
    
    def set_max_failures(self, max_failures: int) -> None:
        """设置最大失败次数阈值"""
        with self._counter_lock:
            self._max_failures = max(1, max_failures)


# 全局单例实例
_tracker = BrowserFailureTracker()


def record_browser_failure() -> int:
    """
    记录一次浏览器启动失败（便捷函数）
    
    Returns:
        当前失败总次数
        
    Raises:
        SystemExit: 如果失败次数超过阈值
    """
    return _tracker.record_failure()


def reset_browser_failure_count() -> None:
    """重置浏览器启动失败计数（便捷函数）"""
    _tracker.reset()


def get_browser_failure_count() -> int:
    """获取当前浏览器启动失败次数（便捷函数）"""
    return _tracker.get_count()