package com.suishoucun.app;

/* 内嵌 CPython 的 JNI 入口。
 * 依赖库与扩展模块都放在 APK 的 lib/<abi>/ 下，由系统安装时解压到
 * nativeLibraryDir，这样 dlopen 才有可执行权限（Android 10+ W^X 限制）。
 */
public final class PyService {

    static {
        System.loadLibrary("crypto_python");
        System.loadLibrary("ssl_python");
        System.loadLibrary("sqlite3_python");
        System.loadLibrary("python3.13");
        System.loadLibrary("pystart");
    }

    private PyService() {
    }

    /** 初始化解释器并运行启动脚本；脚本里的 Flask 服务会一直阻塞到进程结束。 */
    public static native int nativeStart(String home, String paths, String script);
}
