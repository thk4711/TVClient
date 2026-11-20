/* Bounded same-origin JSON requests shared by setup and management. */
async function receiverRequest(path, method = 'GET', data, controller = new AbortController(), timeout = 30000) {
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(path, {method, signal: controller.signal,
            headers: {'Content-Type': 'application/json', 'X-Satip-Action': '1'},
            body: data === undefined ? undefined : JSON.stringify(data)});
        let result;
        try { result = await response.json(); }
        catch (error) {
            if (controller.signal.aborted) throw error;
            throw Error('The receiver returned an unexpected response. Please try again.');
        }
        if (!response.ok) {
            const error = Error(typeof result.detail === 'string' ? result.detail : 'The request could not be completed. Please try again.');
            error.status = response.status; throw error;
        }
        return result;
    } catch (error) {
        if (controller.signal.aborted) throw Error('The request timed out. Check your connection to the receiver and try again.');
        throw error;
    } finally { clearTimeout(timer); }
}
