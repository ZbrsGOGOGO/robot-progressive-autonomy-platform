using System;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.XR;

namespace Robotics.ProgressiveAutonomy
{
    // XR SDKs that implement Unity XR InputDevices can reuse this sample.
    public sealed class PicoInputPublisher : MonoBehaviour
    {
        [Serializable]
        private sealed class PoseValue
        {
            public float[] position = new float[3];
            public float[] rotation = { 0f, 0f, 0f, 1f };
        }

        [Serializable]
        private sealed class Poses
        {
            public PoseValue head = new PoseValue();
            public PoseValue left = new PoseValue();
            public PoseValue right = new PoseValue();
        }

        [Serializable]
        private sealed class Buttons
        {
            public bool left_axis_click, left_primary, left_secondary, left_menu;
            public bool right_axis_click, right_primary, right_secondary, right_menu;
        }

        [Serializable]
        private sealed class InputFrame
        {
            public long sequence;
            public long capture_time_ns;
            public Poses poses = new Poses();
            public bool[] grip = new bool[2];
            public float[] trigger = new float[2];
            public float[] sticks = new float[4];
            public bool tracking;
            public Buttons buttons = new Buttons();
        }

        [SerializeField, Range(1, 30)] private int samplesPerSecond = 20;
        [SerializeField, Range(0.05f, 1f)] private float gripThreshold = 0.5f;
        private readonly InputFrame frame = new InputFrame();
        private UnityWebRequest pending;
        private string endpoint;
        private string bearerToken;
        private bool publishing;
        private double nextSampleTime;

        public long LastResponseCode { get; private set; }

        // Bind after acquiring a session through the workbench or platform SDK.
        // Pass tokens at runtime using local pairing or trusted storage.
        public void Configure(string serverOrigin, string sessionId, string token = null)
        {
            if (!Uri.TryCreate(serverOrigin, UriKind.Absolute, out Uri uri)
                || !(uri.Scheme == Uri.UriSchemeHttps
                    || (uri.Scheme == Uri.UriSchemeHttp && uri.IsLoopback))
                || uri.AbsolutePath != "/" || uri.Query.Length != 0 || uri.Fragment.Length != 0
                || uri.UserInfo.Length != 0)
            {
                throw new ArgumentException("Use an HTTPS origin or local HTTP loopback", nameof(serverOrigin));
            }
            if (string.IsNullOrEmpty(sessionId) || sessionId.Length > 128)
                throw new ArgumentException("A valid session ID is required", nameof(sessionId));
            foreach (char c in sessionId)
            {
                if (!(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z')
                    && !(c >= '0' && c <= '9') && c != '-' && c != '_' && c != '.')
                    throw new ArgumentException("Session ID contains an invalid character", nameof(sessionId));
            }
            if (!string.IsNullOrEmpty(token))
            {
                if (token.Length > 512) throw new ArgumentException("Invalid token", nameof(token));
                foreach (char c in token)
                    if (c < '!' || c > '~') throw new ArgumentException("Invalid token", nameof(token));
            }
            StopSending();
            endpoint = uri.GetLeftPart(UriPartial.Authority)
                + "/v1/sessions/" + Uri.EscapeDataString(sessionId) + "/input";
            bearerToken = token;
        }

        // Publishing input does not arm the robot or create a session.
        public void StartSending()
        {
            if (endpoint == null) throw new InvalidOperationException("Configure a session first");
            publishing = true;
        }

        // This stops client input. A separate platform Hold/Close is required.
        public void StopSending()
        {
            publishing = false;
            if (pending == null) return;
            pending.Abort();
            pending.Dispose();
            pending = null;
        }

        private void OnDisable() => StopSending();

        private void Update()
        {
            if (pending != null)
            {
                if (!pending.isDone) return;
                LastResponseCode = pending.responseCode;
                bool success = pending.result == UnityWebRequest.Result.Success;
                pending.Dispose();
                pending = null;
                if (!success) publishing = false;
            }
            if (!publishing) return;
            double now = Time.realtimeSinceStartupAsDouble;
            if (now < nextSampleTime) return;
            nextSampleTime = now + 1.0 / Mathf.Clamp(samplesPerSecond, 1, 30);

            frame.sequence++;
            frame.capture_time_ns = (long)(now * 1_000_000_000.0);
            InputDevice head = InputDevices.GetDeviceAtXRNode(XRNode.Head);
            InputDevice left = InputDevices.GetDeviceAtXRNode(XRNode.LeftHand);
            InputDevice right = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
            bool headTracked = ReadPose(head, frame.poses.head);
            bool leftTracked = ReadPose(left, frame.poses.left);
            bool rightTracked = ReadPose(right, frame.poses.right);
            frame.tracking = Application.isFocused && headTracked && leftTracked && rightTracked;
            if (frame.tracking)
            {
                ReadControls(left, 0);
                ReadControls(right, 1);
            }
            else
            {
                Array.Clear(frame.grip, 0, 2);
                Array.Clear(frame.trigger, 0, 2);
                Array.Clear(frame.sticks, 0, 4);
                frame.buttons = new Buttons();
            }

            byte[] payload = Encoding.UTF8.GetBytes(JsonUtility.ToJson(frame));
            pending = new UnityWebRequest(endpoint, "POST");
            pending.uploadHandler = new UploadHandlerRaw(payload);
            pending.downloadHandler = new DownloadHandlerBuffer();
            pending.timeout = 2;
            pending.redirectLimit = 0;
            pending.SetRequestHeader("Content-Type", "application/json");
            if (!string.IsNullOrEmpty(bearerToken))
                pending.SetRequestHeader("Authorization", "Bearer " + bearerToken);
            pending.SendWebRequest();
        }

        private static bool ReadPose(InputDevice device, PoseValue pose)
        {
            if (!device.isValid
                || !device.TryGetFeatureValue(CommonUsages.isTracked, out bool tracked) || !tracked
                || !device.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 p)
                || !device.TryGetFeatureValue(CommonUsages.deviceRotation, out Quaternion q)
                || !IsFinite(p.x) || !IsFinite(p.y) || !IsFinite(p.z)
                || !IsFinite(q.x) || !IsFinite(q.y) || !IsFinite(q.z) || !IsFinite(q.w))
                return false;
            float squareNorm = q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w;
            if (squareNorm < 0.25f || squareNorm > 2.25f) return false;
            q = q.normalized;
            pose.position[0] = p.x;
            pose.position[1] = p.y;
            pose.position[2] = p.z;
            pose.rotation[0] = q.x;
            pose.rotation[1] = q.y;
            pose.rotation[2] = q.z;
            pose.rotation[3] = q.w;
            return true;
        }

        private void ReadControls(InputDevice device, int side)
        {
            device.TryGetFeatureValue(CommonUsages.grip, out float grip);
            device.TryGetFeatureValue(CommonUsages.trigger, out float trigger);
            device.TryGetFeatureValue(CommonUsages.primary2DAxis, out Vector2 stick);
            device.TryGetFeatureValue(CommonUsages.primary2DAxisClick, out bool axisClick);
            device.TryGetFeatureValue(CommonUsages.primaryButton, out bool primary);
            device.TryGetFeatureValue(CommonUsages.secondaryButton, out bool secondary);
            device.TryGetFeatureValue(CommonUsages.menuButton, out bool menu);
            frame.grip[side] = IsFinite(grip) && grip >= gripThreshold;
            frame.trigger[side] = IsFinite(trigger) ? Mathf.Clamp01(trigger) : 0f;
            frame.sticks[2 * side] = IsFinite(stick.x) ? Mathf.Clamp(stick.x, -1f, 1f) : 0f;
            frame.sticks[2 * side + 1] = IsFinite(stick.y) ? Mathf.Clamp(stick.y, -1f, 1f) : 0f;
            if (side == 0)
            {
                frame.buttons.left_axis_click = axisClick;
                frame.buttons.left_primary = primary;
                frame.buttons.left_secondary = secondary;
                frame.buttons.left_menu = menu;
            }
            else
            {
                frame.buttons.right_axis_click = axisClick;
                frame.buttons.right_primary = primary;
                frame.buttons.right_secondary = secondary;
                frame.buttons.right_menu = menu;
            }
        }

        private static bool IsFinite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
    }
}
