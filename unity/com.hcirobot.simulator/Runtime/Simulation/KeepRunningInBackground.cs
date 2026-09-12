using UnityEngine;

namespace HciRobot.Simulator
{
    /// <summary>
    /// Keeps Play Mode ticking while the editor window is unfocused, so the
    /// simulator keeps serving MJPEG/DIST and moving while the operator watches
    /// the Python console. Attach once per scene (e.g. next to the services).
    /// </summary>
    [DisallowMultipleComponent]
    public sealed class KeepRunningInBackground : MonoBehaviour
    {
        private void Awake()
        {
            Application.runInBackground = true;
        }
    }
}
