#pragma once
// MainThreadPin.h — re-home a lazily created singleton QObject onto the GUI thread.
//
// Function-local-static singletons (`static Foo inst;`) take the thread
// affinity of whichever thread happens to construct them first. When that
// first touch comes from a worker thread that runs no event loop — the
// external MCP server's QThreadPool, QtConcurrent — every queued call aimed
// at the object afterwards (QMetaObject::invokeMethod(obj, …, QueuedConnection),
// AutoConnection slots, AsyncDispatch::callback_to_promise bodies) is posted
// to a thread that never processes events, so it silently never runs and the
// caller hangs until its watchdog fires.
//
// Call this right after construction, from instance():
//
//     Foo& Foo::instance() {
//         static Foo inst;
//         static const bool pinned = (fincept::pin_singleton_to_main_thread(&inst), true);
//         Q_UNUSED(pinned);
//         return inst;
//     }
//
// It is a no-op when the object was constructed on the main thread.
#include <QCoreApplication>
#include <QObject>
#include <QThread>

namespace fincept {

inline void pin_singleton_to_main_thread(QObject* obj) {
    auto* app = QCoreApplication::instance();
    if (!app || !obj)
        return;
    QThread* main = app->thread();
    // moveToThread() may only be called from the object's current thread,
    // which immediately after construction is the calling thread. Children
    // parented to the object move with it.
    if (obj->thread() != main && obj->thread() == QThread::currentThread())
        obj->moveToThread(main);
}

} // namespace fincept
